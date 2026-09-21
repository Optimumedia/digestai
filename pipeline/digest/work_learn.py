"""AI at Work: what readers teach the order of the cards.

The rules score (work.usefulness) says what a small team *should* find useful. Readers say what they
actually try: on /work pages every card sends card_view when half of it is on screen, and try,
copy_prompt and expand when it is used (site/public/app.js). This module turns those events into a
learned score and blends it into the rules score, carefully, because traffic is small (about 30
visitors a day): a handful of clicks must never reorder the section.

The model, per tool (events carry the card's tool name in `detail`):

- Actions per view. A = tries + 0.8 x prompt copies + 0.4 x how-tos opened, over card_views, both
  counted over the last WINDOW_DAYS days, each event weighted 0.5 ** (age / HALF_LIFE_DAYS).
- Empirical-Bayes shrinkage. The global rate p0 = sum A / sum views over every card. Each group of
  cards (job, cost kind, effort, maker) gets a Beta prior with mean p0; the group's rate is
  p_g = (A_g + M_g p0) / (V_g + M_g). A card's prior mean m is the mean of its groups' rates, and
  its rate is r = (A + M_c m) / (V + M_c). Prior strengths are in views, and never fewer than
  PRIOR_ACTIONS / p0 views, i.e. the prior is worth at least PRIOR_ACTIONS actions: a rare action
  (a 3% rate) needs many views before one click means anything.
- The learned score L = U + SCALE x soft(log2(r / p0)), where U is the rules score and soft() takes
  DEAD_BAND off either side of zero (a lift within about 15% counts as no lift) and clamps at +-2.
- The blend: score = (1 - w) U + w L = U + w SCALE soft(...), with w = min(W_CAP, n / (n + K)) and
  n = the card's own (weighted) views plus GROUP_SHARE x its groups' mean views. Below MIN_VIEWS card
  views in the window, w = 0 for every card: the order is exactly the rules order.

The hard rules are untouched: the blended score only replaces the sort key. featurable() still
decides the featured pick, skip reasons, the hub gate and the plain-language rules still apply.

Egress: one grouped query, one row per tool with eight numbers (at most MAX_ROWS rows), read at
most once every REFRESH_HOURS hours; the result is kept in the runner cache (cache.WORK_EVENTS).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import timedelta

from sqlalchemy import Float, case, func, literal, select

from . import cache, db
from . import work as work_rules
from .trackers import org_key

log = logging.getLogger("digest.work_learn")

WINDOW_DAYS = 30
HALF_LIFE_DAYS = 14.0
ACTION_WEIGHTS = {"try": 1.0, "copy_prompt": 0.8, "expand": 0.4}
LEARN_TYPES = ("card_view", *ACTION_WEIGHTS)
MIN_VIEWS = 200           # card views in the window before any learning counts
PRIOR_ACTIONS = 10.0      # every prior is worth at least this many actions...
M_GROUP_MIN = 100.0       # ...and at least this many views (a group's prior)
M_CARD_MIN = 50.0         # (a card's prior, centred on its groups)
GROUP_SHARE = 0.1         # how much a card's groups' views count as evidence for the card itself
K = 50.0                  # w = n / (n + K)
W_CAP = 0.6               # the rules always keep at least 40% of the say
SCALE = 1.0               # usefulness points per doubling of the action rate
DEAD_BAND = 0.2           # |log2 lift| below this is noise
LIFT_CLAMP = 2.0          # at most 4x (or a quarter) counts
REFRESH_HOURS = 6.0
MAX_ROWS = 500
SENTENCE_MIN_VIEWS = 100  # a group needs this many raw views before the admin page names it
MOVERS_DAYS = 7           # the admin page's movers: the cards of the last week, as the home page shows them


def tool_key(tool: str | None) -> str:
    """The key events and cards share: the tool's name as the page writes it into data-work-tool,
    whitespace and case folded, cut at the events table's 100 characters."""
    return " ".join(str(tool or "").split()).lower()[:100]


# ---------------------------------------------------------------------------- the read

def _weight_expr(col, now):
    """0.5 ** (age / HALF_LIFE_DAYS), in whole-day steps (each day at its midpoint), as a CASE the
    database evaluates: portable across SQLite and Postgres, and nothing but the sums comes back."""
    whens = [(col >= now - timedelta(days=d + 1), literal(round(0.5 ** ((d + 0.5) / HALF_LIFE_DAYS), 4), Float))
             for d in range(WINDOW_DAYS)]
    return case(*whens, else_=literal(0.0, Float))


def read_counts(conn, now) -> list[tuple]:
    """Per tool, over the last WINDOW_DAYS days: (detail, views, tries, copies, expands, and the
    same four recency-weighted). One grouped query, one row per tool, at most MAX_ROWS rows."""
    e = db.events.c
    since = now - timedelta(days=WINDOW_DAYS)
    inner = (select(e.detail.label("detail"), e.type.label("type"), _weight_expr(e.created_at, now).label("w"))
             .where(e.created_at >= since, e.type.in_(LEARN_TYPES), e.detail.isnot(None), e.detail != "")
             .subquery())
    cols = []
    for t in LEARN_TYPES:
        cols.append(func.sum(case((inner.c.type == t, 1), else_=0)))
    for t in LEARN_TYPES:
        cols.append(func.sum(case((inner.c.type == t, inner.c.w), else_=literal(0.0, Float))))
    views = cols[0]
    rows = conn.execute(select(inner.c.detail, *cols).group_by(inner.c.detail)
                        .order_by(views.desc(), inner.c.detail).limit(MAX_ROWS)).all()
    return [(str(r[0]), *[int(x or 0) for x in r[1:5]], *[round(float(x or 0), 4) for x in r[5:9]]) for r in rows]


def counts(conn, now, max_age_hours: float = REFRESH_HOURS) -> tuple[dict[str, dict], dict]:
    """The per-tool counts, from the runner's copy when it is younger than max_age_hours, else read
    once and kept. Returns (by tool key, info) where info says whether the database was asked and
    how many kilobytes that read cost."""
    st = cache.WORK_EVENTS.get(conn)
    info = {"fromCache": True, "readKB": 0.0, "at": st.get("at")}
    fresh = st.get("rows") is not None and time.time() - float(st.get("at") or 0) < max_age_hours * 3600
    if not fresh:
        b0 = db.bytes_read()
        rows = read_counts(conn, now)
        cache.WORK_EVENTS.put(conn, {"at": time.time(), "rows": [list(r) for r in rows]})
        info = {"fromCache": False, "readKB": round((db.bytes_read() - b0) / 1024, 1), "at": time.time()}
        st = cache.WORK_EVENTS.get(conn)
    out: dict[str, dict] = {}
    for r in st.get("rows") or []:
        key = tool_key(r[0])
        row = out.setdefault(key, {"views": 0, "try": 0, "copy_prompt": 0, "expand": 0,
                                   "wviews": 0.0, "wtry": 0.0, "wcopy_prompt": 0.0, "wexpand": 0.0})
        for i, t in enumerate(LEARN_TYPES):
            name = "views" if t == "card_view" else t
            row[name] += int(r[1 + i])
            row["w" + name] += float(r[5 + i])
    info["rows"] = len(st.get("rows") or [])
    return out, info


# ---------------------------------------------------------------------------- the model (pure)

def _actions(row: dict) -> float:
    """Recency-weighted tries, prompt copies (x0.8) and how-tos opened (x0.4)."""
    return sum(ACTION_WEIGHTS[t] * row.get("w" + t, 0.0) for t in ACTION_WEIGHTS)


def cost_group(card: dict) -> str:
    """Free / free to try / included in a plan / paid / not stated, as the card's label says it."""
    label = work_rules.cost_label({"cost": card.get("cost"), "included_in": card.get("includedIn")})
    if label == "Free":
        return "free"
    if label == "Free to try":
        return "free to try"
    if label.startswith(("Included", "Already")):
        return "included"
    if label.startswith("Paid"):
        return "paid"
    return "not stated"


def groups_of(card: dict) -> list[tuple[str, str]]:
    """The groups a card is pooled with: its jobs, its cost kind, its effort and its maker."""
    out = [("job", j) for j in card.get("jobs") or []]
    out.append(("cost", cost_group(card)))
    out.append(("effort", card.get("effort") or "not stated"))
    if card.get("maker") and org_key(card["maker"]):
        out.append(("maker", org_key(card["maker"])))
    return out


def _soft(x: float) -> float:
    x = max(-LIFT_CLAMP, min(LIFT_CLAMP, x))
    return math.copysign(max(0.0, abs(x) - DEAD_BAND), x)


def model(by_tool: dict[str, dict], stories: list[dict]) -> dict:
    """The learned rate of every card in `stories` (exported shape, with workCard), pooled by group.

    Returns {"active", "views", "actions", "p0", "cards": {tool key: {...}}, "groups": {...}}. With
    fewer than MIN_VIEWS card views, "active" is False and every card's weight is 0."""
    cards: dict[str, dict] = {}
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or ""):
        c = s.get("workCard")
        if c:
            cards[tool_key(c["tool"])] = c  # the newest story's card describes the tool
    zero = {"views": 0, "wviews": 0.0, "try": 0, "copy_prompt": 0, "expand": 0, "wtry": 0.0, "wcopy_prompt": 0.0, "wexpand": 0.0}
    stats = {k: by_tool.get(k, zero) for k in cards}
    raw_views = sum(r["views"] for r in stats.values())
    V = sum(r["wviews"] for r in stats.values())
    A = sum(min(_actions(r), r["wviews"]) for r in stats.values())
    out = {"active": raw_views >= MIN_VIEWS and V > 0 and A > 0, "views": raw_views,
           "actions": sum(r["try"] + r["copy_prompt"] for r in stats.values()),
           "p0": (A / V) if V else 0.0, "cards": {}, "groups": {}}
    if not out["active"]:
        out["cards"] = {k: {"w": 0.0, "lift": 1.0, "rate": None, "views": stats[k]["views"]} for k in cards}
        return out
    p0 = A / V
    m_group = max(M_GROUP_MIN, PRIOR_ACTIONS / p0)
    m_card = max(M_CARD_MIN, PRIOR_ACTIONS / p0)
    groups: dict[tuple[str, str], dict] = {}
    for k, c in cards.items():
        r = stats[k]
        for g in groups_of(c):
            gr = groups.setdefault(g, {"A": 0.0, "V": 0.0, "views": 0, "actions": 0, "cards": 0, "label": _group_label(g, c)})
            gr["A"] += min(_actions(r), r["wviews"])
            gr["V"] += r["wviews"]
            gr["views"] += r["views"]
            gr["actions"] += r["try"] + r["copy_prompt"]
            gr["cards"] += 1
    for gr in groups.values():
        gr["rate"] = (gr["A"] + m_group * p0) / (gr["V"] + m_group)
        gr["lift"] = gr["rate"] / p0
    for k, c in cards.items():
        r = stats[k]
        gs = [groups[g] for g in groups_of(c)]
        prior = sum(g["rate"] for g in gs) / len(gs)
        rate = (min(_actions(r), r["wviews"]) + m_card * prior) / (r["wviews"] + m_card)
        n = r["wviews"] + GROUP_SHARE * (sum(g["V"] for g in gs) / len(gs))
        out["cards"][k] = {"w": round(min(W_CAP, n / (n + K)), 4), "lift": round(rate / p0, 4), "rate": rate,
                           "views": r["views"]}
    out["groups"] = {f"{d}:{v}": g for (d, v), g in groups.items()}
    return out


def blended(usefulness: float, learned: dict | None) -> float:
    """The rules score moved by what readers did: U + w SCALE soft(log2 lift). Exactly U without data."""
    if not learned or learned["w"] <= 0 or learned["lift"] <= 0:
        return usefulness
    shift = learned["w"] * SCALE * _soft(math.log2(learned["lift"]))
    return round(usefulness + shift, 3) if shift else usefulness


def apply(stories: list[dict], learned: dict) -> int:
    """Blend the learned score into every exported card's usefulness, keeping the rules score as
    rulesUsefulness where the two differ. Returns how many cards moved."""
    moved = 0
    for s in stories:
        c = s.get("workCard")
        if not c:
            continue
        info = learned["cards"].get(tool_key(c["tool"]))
        score = blended(c["usefulness"], info)
        if score != c["usefulness"]:
            c["rulesUsefulness"] = c["usefulness"]
            c["usefulness"] = score
            c["learnedWeight"] = info["w"]
            moved += 1
    return moved


# ---------------------------------------------------------------------------- what it taught

COST_WORDS = {"free": "Free cards", "free to try": "Free-to-try cards", "included": "Cards already included in a plan",
              "paid": "Paid cards", "not stated": "Cards with no stated price"}
EFFORT_WORDS = {"minutes": "Few-minutes cards", "an afternoon": "Afternoon-sized cards",
                "needs a developer": "Cards that need a developer", "not stated": "Cards with no stated setup time"}


def _group_label(g: tuple[str, str], card: dict) -> str:
    dim, value = g
    if dim == "job":
        return f"“{work_rules.JOBS.get(value, value)}” cards"
    if dim == "cost":
        return COST_WORDS.get(value, value)
    if dim == "effort":
        return EFFORT_WORDS.get(value, value)
    return f"{card.get('maker') or value} cards"


def _times(x: float) -> str:
    return f"{x:.1f}×"


def taught(learned: dict, stories: list[dict], rules_order: list[int], order: list[int]) -> dict:
    """The admin page's "What readers taught the order": up to three groups tried most often against
    the average (the shrunk rate, so never more than the data says; the raw counts in brackets), how
    much of the order is reader data today, and the cards that moved most. `rules_order` and `order`
    are the same stories' ids, by the rules score and by the blended one."""
    out = {"active": learned["active"], "views": learned["views"], "minViews": MIN_VIEWS, "windowDays": WINDOW_DAYS,
           "halfLifeDays": HALF_LIFE_DAYS, "cap": W_CAP, "sentences": [], "movers": []}
    if not learned["active"]:
        out["text"] = (f"Not enough reader data yet ({learned['views']:,} card views; learning starts to count "
                       f"from about {MIN_VIEWS}).")
        return out
    groups = sorted((g for g in learned["groups"].values()
                     if g["views"] >= SENTENCE_MIN_VIEWS and round(g["lift"], 1) >= 1.2),
                    key=lambda g: (-g["lift"], g["label"]))
    for g in groups[:3]:
        out["sentences"].append(f"{g['label']} are tried {_times(g['lift'])} as often as the average card "
                                f"({g['actions']:,} tries and prompt copies in {g['views']:,} views).")
    if not out["sentences"]:
        out["sentences"].append("No kind of card stands out from the average yet.")
    by_id = {s["id"]: s for s in stories}
    ws = [learned["cards"].get(tool_key(by_id[i]["workCard"]["tool"]), {}).get("w", 0.0) for i in order if i in by_id]
    out["dataShare"] = round(100 * sum(ws) / len(ws)) if ws else 0
    before = {sid: i + 1 for i, sid in enumerate(rules_order)}
    moves = [(before[sid] - (i + 1), sid, before[sid], i + 1) for i, sid in enumerate(order) if sid in before]
    ups = sorted((m for m in moves if m[0] > 0), key=lambda m: (-m[0], m[3]))[:2]
    downs = sorted((m for m in moves if m[0] < 0), key=lambda m: (m[0], m[3]))[:2]
    for delta, sid, was, now_at in ups + downs:
        s = by_id[sid]
        out["movers"].append({"id": sid, "slug": s.get("slug"), "headline": s["workCard"].get("headline") or s.get("headline"),
                              "tool": s["workCard"]["tool"], "from": was, "to": now_at})
    out["text"] = (f"{out['dataShare']}% of the order comes from reader data today (at most {round(100 * W_CAP)}%), "
                   f"from {learned['views']:,} card views in {WINDOW_DAYS} days.")
    return out


def changed(rules_briefing: dict, briefing: dict, stories: list[dict]) -> dict | None:
    """What reader data changed at the top of the section briefing: the featured pick, else the first
    card that entered the top three. None when the top three and the pick are the rules' own."""
    by_id = {s["id"]: s for s in stories}

    def head(i):
        s = by_id.get(i) or {}
        return (s.get("workCard") or {}).get("headline") or s.get("headline")

    if briefing.get("featuredId") != rules_briefing.get("featuredId") and briefing.get("featuredId") is not None:
        return {"kind": "featured", "to": head(briefing["featuredId"]),
                "from": head(rules_briefing["featuredId"]) if rules_briefing.get("featuredId") is not None else None}
    top, was = briefing["storyIds"][:3], rules_briefing["storyIds"]
    for pos, sid in enumerate(top, 1):
        if pos > len(was) or was[pos - 1] != sid:
            return {"kind": "top3", "to": head(sid), "at": pos, "was": was.index(sid) + 1 if sid in was else None}
    return None


def learn(stories: list[dict], conn, now, section: list[dict]) -> tuple[dict, dict]:
    """Read (or reuse) the counts, model them over the section's cards and blend the result into
    every exported card. Returns (learned model, stats for the export step)."""
    by_tool, info = counts(conn, now)
    learned = model(by_tool, section)
    moved = apply(stories, learned) if learned["active"] else 0
    stats = {"readKB": info["readKB"], "fromCache": info["fromCache"], "rows": info.get("rows", 0),
             "views": learned["views"], "active": learned["active"], "moved": moved}
    return learned, stats
