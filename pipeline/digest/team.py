"""The AI team: six roles that read what the run already measured and keep the site moving.

Martin asked (23 Nov 2026) for a team on the admin page, each role with its own job, and a picture
of how they work together. Every role is a set of rules over data this run already has (admin.json,
gsc.json, the live site's own API file), so the team adds no database reads. Two roles may ask a free
model for words: the ideas role once a week, and nothing else depends on it.

- Guard (health and truth): is the live site actually changing, did a step crash, are the reader
  numbers honest? Added because from 7 to 23 Nov 2026 every run was green while the export crashed
  and the site stood still for 16 days, and in October a wave of automated visits looked like readers.
- Platform: run time against the limit, failing sources, free model allowances, database and egress.
- Market: where readers come from, what they read and search for, what Google shows us for.
- Ideas: three zero-cost ideas a week no one asked for, grounded in the other roles' facts.
- Finisher: a ledger of every open finding with its age and owner; nothing is dropped quietly. An
  item closes only when the role that raised it runs again and no longer finds it.
- Brain (team lead): reads everyone, tracks readers a day against the week before, and turns the
  ledger into today's three priorities, each with an owner (Martin, Claude, or the pipeline itself).

Each role has its own rhythm (EVERY_HOURS); a role that is not due keeps its last output. The result
goes to site/src/data/team.json (the /admin/team page) and the runner's cache (the ledger's memory).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

from . import config

log = logging.getLogger("digest.team")

FORMAT = 1
# Open items that wait on a person, edited by hand like moderation.yaml: {"items": [{"key", "text",
# "owner", "level", "done"}]}. The Finisher keeps each one open until it is marked done there.
ITEMS_FILE = Path(__file__).with_name("team_items.json")
EVERY_HOURS = {"guard": 0, "platform": 1, "market": 6, "ideas": 24 * 7, "finisher": 0, "brain": 0}
ORDER = ["brain", "guard", "platform", "market", "ideas", "finisher"]
# Who reads whose work: the arrows on the team page.
HANDOFFS = [
    ("guard", "brain", "incidents first"),
    ("platform", "brain", "what to fix"),
    ("market", "brain", "what readers want"),
    ("ideas", "brain", "ideas to try"),
    ("market", "ideas", "facts to build on"),
    ("guard", "finisher", "every finding"),
    ("platform", "finisher", "every finding"),
    ("market", "finisher", "every finding"),
    ("finisher", "brain", "what is overdue"),
    ("brain", "owners", "today's plan"),
]
ROLES = {
    "brain": ("Brain", "Team lead",
              "Reads every role, tracks readers a day against last week, and sets today's three priorities with an owner each."),
    "guard": ("Guard", "Health and truth",
              "Checks the live site is changing, no step crashed, and reader numbers leave automated visits out."),
    "platform": ("Platform", "Platform improvements",
                 "Watches run time, failing sources, free model allowances, database size and egress."),
    "market": ("Market", "Market knowledge",
               "Where readers come from, what they read and search for, and what Google shows the site for."),
    "ideas": ("Ideas", "Out-of-the-box ideas",
              "Once a week, three zero-cost ideas nobody asked for, built on the other roles' facts."),
    "finisher": ("Finisher", "Nothing left unfinished",
                 "Keeps every open finding with its age and owner, and closes it only when it is really gone."),
}
SITE_STALE_HOURS = 3.0
STEP_LIMIT_SECONDS = 20 * 60
SOURCE_ERRORS = 5
OVERDUE_DAYS = 7
KEEP_CLOSED_DAYS = 14
IDEAS_KEPT = 30
LEVELS = {"act": 0, "watch": 1, "info": 2}
ROLE_RANK = {"guard": 0, "platform": 1, "market": 2, "ideas": 3, "finisher": 4, "owner": 5}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def n(x) -> str:
    return f"{int(round(x)):,}"


def finding(key: str, text: str, level: str = "info", owner: str = "pipeline") -> dict:
    return {"key": key, "text": text, "level": level, "owner": owner}


def role_out(rid: str, now: datetime, findings: list[dict], headline: str, **more) -> dict:
    worst = min((LEVELS[f["level"]] for f in findings), default=2)
    status = {0: "act", 1: "watch", 2: "ok"}[worst]
    name, title, job = ROLES[rid]
    return {"id": rid, "name": name, "title": title, "job": job, "ranAt": _iso(now), "status": status,
            "headline": headline, "findings": findings, **more}


def latest_run(admin: dict) -> list[dict]:
    """The step rows of the newest run in admin.json (a run starts with fetch)."""
    steps = sorted(admin.get("runs") or [], key=lambda s: s.get("startedAt") or "")
    run: list[dict] = []
    for s in steps:
        if s.get("step") == "fetch":
            run = []
        run.append(s)
    return run


def readers_per_day(admin: dict) -> list[tuple[str, int]]:
    """(day, readers) for complete days, oldest first: the engagement's readers-only visitors."""
    rows = ((admin.get("engagement") or {}).get("perDay")) or []
    gaps = {d for g in admin.get("counterGaps") or [] for d in _days(g.get("from"), g.get("to"))}
    out = [(r["day"], int(r.get("visitors") or 0)) for r in rows if r.get("day") and r["day"] not in gaps]
    return out[:-1] if out else out  # the last day is today, still running


def _days(a, b) -> list[str]:
    try:
        d, end = datetime.fromisoformat(a).date(), datetime.fromisoformat(b).date()
    except (TypeError, ValueError):
        return []
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------- the roles

def guard(admin: dict, now: datetime, site_hours: float | None) -> dict:
    f: list[dict] = []
    if site_hours is not None and site_hours > SITE_STALE_HOURS:
        f.append(finding("site-stale", f"The live site was last generated {site_hours:.1f} hours ago: readers see old news.",
                         "act", "Claude"))
    crashed = [s["step"] for s in latest_run(admin) if (s.get("stats") or {}).get("crashed")]
    if crashed:
        f.append(finding("crashed:" + ",".join(sorted(crashed)),
                         f"Crashed in the latest run: {', '.join(crashed)}.", "act", "Claude"))
    days = readers_per_day(admin)
    if len(days) >= 2 and all(v == 0 for _d, v in days[-2:]) and any(v for _d, v in days[:-2]):
        f.append(finding("counter-silent", "No reader was counted for two days: the visitor counter may be blocked.",
                         "act", "Claude"))
    eng = admin.get("engagement") or {}
    auto = int(((eng.get("automated7") or {}).get("visitors")) or 0)
    week = sum(v for _d, v in days[-7:])
    if auto and auto > 3 * max(week, 1):
        f.append(finding("bot-wave", f"Automated visits outnumber readers this week, {n(auto)} to {n(week)}. "
                                     "Reader numbers leave them out.", "watch", "pipeline"))
    cards = [c for c in admin.get("actions") or [] if c.get("level") in ("critical", "warning")]
    if cards:
        f.append(finding("admin-cards", f"{len(cards)} {'check needs' if len(cards) == 1 else 'checks need'} a look on the Today tab.",
                         "watch", "Martin"))
    head = f[0]["text"] if f else "The site is live and changing, no step crashed, and reader numbers are clean."
    return role_out("guard", now, f, head, siteHours=site_hours)


def platform(admin: dict, now: datetime) -> dict:
    f: list[dict] = []
    run = latest_run(admin)
    seconds = sum(float((s.get("stats") or {}).get("seconds") or 0) for s in run)
    if seconds > STEP_LIMIT_SECONDS * 0.9:
        f.append(finding("run-time", f"The latest run took {seconds / 60:.0f} of its {STEP_LIMIT_SECONDS // 60} minutes: "
                                     "the next slow day cuts steps off.", "act", "Claude"))
    elif seconds > STEP_LIMIT_SECONDS * 0.75:
        f.append(finding("run-time", f"The latest run took {seconds / 60:.0f} of its {STEP_LIMIT_SECONDS // 60} minutes.",
                         "watch", "Claude"))
    failing = [s for s in admin.get("sources") or [] if s.get("enabled") and not s.get("discovered")
               and int(s.get("errorCount") or 0) >= SOURCE_ERRORS]
    if failing:
        names = ", ".join(s.get("name") or s.get("key") for s in failing[:4])
        more = f" and {len(failing) - 4} more" if len(failing) > 4 else ""
        f.append(finding("sources-failing", f"{len(failing)} {'source keeps' if len(failing) == 1 else 'sources keep'} failing: {names}{more}.",
                         "watch", "Claude"))
    today = now.date().isoformat()
    spent = sorted({u["provider"] for u in (admin.get("llm") or {}).get("usage") or []
                    if u.get("day") == today and u.get("exhausted")})
    if spent:
        f.append(finding("models-spent", f"Free model allowance used up today: {', '.join(spent)}. Summaries wait for the next day's allowance.",
                         "watch" if len(spent) >= 2 else "info", "pipeline"))
    dbs = admin.get("database") or {}
    size, limit = dbs.get("sizeMB"), dbs.get("limitMB")
    if size and limit and size > 0.8 * limit:
        f.append(finding("db-size", f"The database holds {size:.0f} of {limit:.0f} MB.", "act" if size > 0.95 * limit else "watch", "Claude"))
    projected = ((dbs.get("cycle") or {}).get("projectedMB"))
    quota = ((dbs.get("reads") or {}).get("quotaMB"))
    if projected and quota and projected > 0.8 * quota:
        f.append(finding("egress", f"Database reads are heading for {projected:,.0f} of {quota:,.0f} MB this cycle.",
                         "act" if projected > quota else "watch", "Claude"))
    head = f[0]["text"] if f else f"Runs take {seconds / 60:.0f} of {STEP_LIMIT_SECONDS // 60} minutes; sources, models and database are within limits."
    return role_out("platform", now, f, head, runMinutes=round(seconds / 60, 1))


def market(admin: dict, gsc: dict | None, now: datetime) -> dict:
    f: list[dict] = []
    eng = admin.get("engagement") or {}
    refs = [s for s in eng.get("sources7") or [] if s.get("name") not in ("Direct", None) and s.get("visitors")]
    if refs:
        f.append(finding("referrers", "Readers this week came from " + ", ".join(f"{s['name']} ({s['visitors']})" for s in refs[:4]) + "."))
    searches = (eng.get("searches7") or {})
    missing = [s["query"] for s in searches.get("missing") or []][:4]
    if missing:
        f.append(finding("search-missing", "Readers searched the site and found nothing for: " + ", ".join(f'"{q}"' for q in missing) + ".",
                         "watch", "Claude"))
    top = [s["query"] for s in searches.get("top") or [] if s.get("results")][:4]
    if top:
        f.append(finding("search-top", "Most searched on the site: " + ", ".join(f'"{q}"' for q in top) + "."))
    pages = [p for p in eng.get("pages7") or [] if p.get("path") not in ("/", "/index.html")][:3]
    if pages:
        f.append(finding("pages-read", "Most read pages: " + ", ".join(p["path"] for p in pages) + "."))
    if gsc and gsc.get("queries"):
        q = sorted(gsc["queries"], key=lambda r: -float(r.get("impressions") or 0))
        shown = [r for r in q if float(r.get("impressions") or 0) >= 20 and not float(r.get("clicks") or 0)][:3]
        if shown:
            f.append(finding("search-shown", "Google shows us for " + ", ".join(f'"{r["query"]}"' for r in shown)
                             + " without a click: those pages need better titles or answers.", "watch", "Claude"))
        clicks = sum(float(r.get("clicks") or 0) for r in gsc.get("perDay") or [])
        f.append(finding("search-clicks", f"Google sent {n(clicks)} clicks from {gsc.get('start')} to {gsc.get('end')}."))
    head = f[0]["text"] if f else "Not enough reader data this week to say where readers come from."
    return role_out("market", now, f, head)


IDEAS_PROMPT = """You are the ideas member of a small team growing digestai.news, a free AI news site
that merges every outlet reporting the same news into one story with all sources linked.
Facts from the team this week:
{facts}

Propose exactly 3 unconventional ideas to bring more real readers, each doable at zero cost by a
developer in a day, without paid ads, fake accounts, spam or invented claims. Do not repeat these
earlier ideas: {seen}.
Answer only with JSON: {{"ideas": [{{"title": "...", "why": "...", "firstStep": "..."}}]}}
Plain words, no hype, one sentence each for why and firstStep. Use no numbers that are not in the facts."""


def ideas(facts: list[str], prev: dict | None, now: datetime, call=None) -> dict:
    """Three ideas a week from a free model, or the last week's when no model answered."""
    seen = [i["title"] for i in (prev or {}).get("log") or []]
    log_ = list((prev or {}).get("log") or [])
    got: list[dict] = []
    reason = None
    if call is None:
        reason = "no free model available"
    else:
        try:
            result = call(IDEAS_PROMPT.format(facts="\n".join(f"- {x}" for x in facts) or "- (none yet)",
                                              seen="; ".join(seen[-IDEAS_KEPT:]) or "none"))
            allowed = set(re.findall(r"\d[\d,.]*", " ".join(facts)))
            for i in (result or {}).get("ideas") or []:
                if not isinstance(i, dict) or not all(isinstance(i.get(k), str) and i[k].strip() for k in ("title", "why", "firstStep")):
                    continue
                text = " ".join(i[k] for k in ("title", "why", "firstStep"))
                if set(re.findall(r"\d[\d,.]*", text)) - allowed or i["title"].strip() in seen:
                    continue  # a number nobody measured, or an idea already made
                got.append({k: i[k].strip()[:300] for k in ("title", "why", "firstStep")})
            got = got[:3]
            if not got:
                reason = "the model's ideas did not pass the checks"
        except Exception as exc:  # noqa: BLE001 - ideas are never worth a step
            reason = f"model failed: {str(exc)[:80]}"
    if got:
        log_ = (log_ + [{"title": i["title"], "at": _iso(now)} for i in got])[-IDEAS_KEPT:]
        f = [finding("idea:" + i["title"].lower()[:60], f"{i['title']}: {i['why']} First step: {i['firstStep']}", "info", "Claude") for i in got]
        out = role_out("ideas", now, f, f"This week: {got[0]['title']}.", ideas=got, log=log_)
        return out
    old = (prev or {}).get("ideas") or []
    f = [finding("idea:" + i["title"].lower()[:60], f"{i['title']}: {i['why']} First step: {i['firstStep']}", "info", "Claude") for i in old]
    out = role_out("ideas", now, f, f"No new ideas this week ({reason})." if not old else f"Last week's ideas stand ({reason}).",
                   ideas=old, log=log_)
    out["status"] = "idle" if not old else out["status"]
    return out


def finisher(roles: dict[str, dict], ran: set[str], ledger: dict, owner_items: list[dict], now: datetime) -> tuple[dict, dict]:
    """The ledger: every act or watch finding, kept open until the role that raised it runs again
    without it. Owner items (team_items.json) stay open until marked done there."""
    led = {k: dict(v) for k, v in (ledger or {}).items()}
    stamp = _iso(now)
    live: dict[str, tuple[str, dict]] = {}
    for rid, r in roles.items():
        for x in r.get("findings") or []:
            if x["level"] in ("act", "watch"):
                live[f"{rid}:{x['key']}"] = (rid, x)
    for item in owner_items:
        if item.get("key") and item.get("text") and not item.get("done"):
            live[f"owner:{item['key']}"] = ("owner", finding(item["key"], item["text"], item.get("level") or "watch",
                                                             item.get("owner") or "Martin"))
    for key, (rid, x) in live.items():
        row = led.get(key)
        if row is None or row.get("closedAt"):
            led[key] = {"key": key, "role": rid, "text": x["text"], "level": x["level"], "owner": x["owner"],
                        "openedAt": stamp, "seenAt": stamp, "closedAt": None}
        else:
            row.update(text=x["text"], level=x["level"], owner=x["owner"], seenAt=stamp)
    done_owner = {f"owner:{i['key']}" for i in owner_items if i.get("key") and i.get("done")}
    for key, row in led.items():
        if row.get("closedAt") or key in live:
            continue
        if row["role"] in ran or key in done_owner or (row["role"] == "owner" and key not in live):
            row["closedAt"] = stamp
    keep = now - timedelta(days=KEEP_CLOSED_DAYS)
    led = {k: v for k, v in led.items() if not v.get("closedAt") or (_parse(v["closedAt"]) or now) >= keep}

    def age(v) -> float:
        return (now - (_parse(v["openedAt"]) or now)).total_seconds() / 86400

    open_ = sorted((v for v in led.values() if not v.get("closedAt")), key=lambda v: (LEVELS[v["level"]], -age(v)))
    overdue = [v for v in open_ if age(v) >= OVERDUE_DAYS]
    week = now - timedelta(days=7)
    closed = [v for v in led.values() if v.get("closedAt") and (_parse(v["closedAt"]) or now) >= week]
    f = [finding("overdue:" + v["key"], f"Open {age(v):.0f} days ({v['owner']}): {v['text']}", "act" if v["level"] == "act" else "watch",
                 v["owner"]) for v in overdue[:5]]
    head = (f"{len(open_)} open, {len(overdue)} older than a week, {len(closed)} closed in the last 7 days."
            if led else "Nothing open.")
    items = [{**v, "ageDays": round(age(v), 1)} for v in open_]
    items += sorted(({**v, "ageDays": round(age(v), 1)} for v in closed), key=lambda v: v["closedAt"], reverse=True)
    return role_out("finisher", now, f, head, items=items, open=len(open_), overdue=len(overdue), closed7=len(closed)), led


def brain(roles: dict[str, dict], ledger: dict, admin: dict, now: datetime) -> dict:
    days = readers_per_day(admin)
    week = [v for _d, v in days[-7:]]
    before = [v for _d, v in days[-14:-7]]
    goal: dict = {}
    lines: list[str] = []
    if week:
        avg = sum(week) / len(week)
        goal = {"readersPerDay": round(avg, 1), "days": len(week)}
        if len(before) == 7 and sum(before):
            prev = sum(before) / 7
            change = (avg - prev) / prev * 100
            goal.update(previous=round(prev, 1), changePct=round(change))
            lines.append(f"Readers: {avg:.0f} a day this week, {'up' if change >= 0 else 'down'} {abs(change):.0f}% on last week ({prev:.0f}).")
        else:
            lines.append(f"Readers: {avg:.0f} a day this week.")

    def age(v) -> float:
        return (now - (_parse(v["openedAt"]) or now)).total_seconds() / 86400

    open_ = [v for v in (ledger or {}).values() if not v.get("closedAt")]
    open_.sort(key=lambda v: (LEVELS[v["level"]], ROLE_RANK.get(v["role"], 9), -age(v)))
    plan = [{"text": v["text"], "owner": v["owner"], "role": v["role"], "level": v["level"], "ageDays": round(age(v), 1)}
            for v in open_[:3]]
    idea = next(iter((roles.get("ideas") or {}).get("ideas") or []), None)
    if len(plan) < 3 and idea:
        plan.append({"text": f"Try an idea: {idea['title']}. {idea['firstStep']}", "owner": "Claude", "role": "ideas", "level": "info", "ageDays": 0})
    if plan:
        lines.append("Today: " + " ".join(f"{i}. {p['text']} ({p['owner']})" for i, p in enumerate(plan, 1)))
    else:
        lines.append("Nothing urgent: keep publishing and look at the ideas.")
    act = [r["name"] for r in roles.values() if r.get("status") == "act"]
    if act:
        lines.append(f"Needs action now: {', '.join(act)}.")
    f = [finding(f"plan:{i}", p["text"], p["level"] if p["level"] in LEVELS else "info", p["owner"]) for i, p in enumerate(plan, 1)]
    head = lines[0] if lines else "Waiting for reader data."
    return role_out("brain", now, f, head, plan=plan, goal=goal, brief=lines)


# ---------------------------------------------------------------------------- the step

def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def due(prev: dict | None, rid: str, now: datetime) -> bool:
    ran = _parse(((prev or {}).get("roles") or {}).get(rid, {}).get("ranAt"))
    return ran is None or (now - ran).total_seconds() >= EVERY_HOURS[rid] * 3600 - 300


def facts_for_ideas(roles: dict[str, dict]) -> list[str]:
    out = []
    for rid in ("brain", "market", "platform", "guard"):
        r = roles.get(rid) or {}
        out += [x["text"] for x in r.get("findings") or []][:4]
        if rid == "brain":
            out += (r.get("brief") or [])[:1]
    return [x for x in out if x][:14]


def build(admin: dict, gsc: dict | None, prev: dict | None, now: datetime, site_hours: float | None,
          owner_items: list[dict] | None = None, call=None) -> dict:
    """The whole team for this run; pure, so it can be tested without the network."""
    old = (prev or {}).get("roles") or {}
    roles: dict[str, dict] = {}
    ran: set[str] = set()

    def take(rid, fn):
        if due(prev, rid, now) or rid not in old:
            roles[rid] = fn()
            ran.add(rid)
        else:
            roles[rid] = old[rid]

    take("guard", lambda: guard(admin, now, site_hours))
    take("platform", lambda: platform(admin, now))
    take("market", lambda: market(admin, gsc, now))
    roles["brain"] = old.get("brain") or brain(roles, {}, admin, now)
    take("ideas", lambda: ideas(facts_for_ideas({**roles, "brain": roles["brain"]}), old.get("ideas"), now, call))
    roles["finisher"], ledger = finisher({k: v for k, v in roles.items() if k in ran}, ran, (prev or {}).get("ledger") or {},
                                         owner_items or [], now)
    ran.add("finisher")
    roles["brain"] = brain(roles, ledger, admin, now)
    ran.add("brain")
    return {"format": FORMAT, "generatedAt": _iso(now), "order": ORDER,
            "handoffs": [{"from": a, "to": b, "what": w} for a, b, w in HANDOFFS],
            "roles": {rid: roles[rid] for rid in ORDER}, "ranNow": sorted(ran), "ledger": ledger}


def _model_call(info: dict):
    """The ideas role's free model: the morning note's (Groq, within its daily allowance)."""
    from . import morning

    return morning._default_call(info)


def run() -> dict:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    cache_path = config.CACHE_DIR / "team.json"
    prev = _read(cache_path, None)
    admin = _read(config.SITE_DATA_DIR / "admin.json", None) or _read(config.CACHE_DIR / "admin.json", {})
    gsc = _read(config.SITE_DATA_DIR / "gsc.json", None)
    owner_items = _read(ITEMS_FILE, {}).get("items") or []
    site_hours = None
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("TEAM_SITE_CHECK") == "1":
        from . import watchdog

        site_hours = watchdog.site_age(now)
    info: dict = {}
    call = None
    if due(prev, "ideas", now):
        try:
            call = _model_call(info)
        except Exception as exc:  # noqa: BLE001 - no model, no ideas
            info["reason"] = str(exc)[:80]
    team = build(admin or {}, gsc, prev, now, site_hours, owner_items, call)
    text = json.dumps(team, ensure_ascii=False, separators=(",", ":"))
    for path in (config.SITE_DATA_DIR / "team.json", cache_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    status = {rid: r["status"] for rid, r in team["roles"].items()}
    return {"ran": team["ranNow"], "status": status, "open": team["roles"]["finisher"].get("open"),
            "siteHours": site_hours, "model": info.get("provider") or info.get("reason"), "seconds": round(time.monotonic() - t0, 1)}
