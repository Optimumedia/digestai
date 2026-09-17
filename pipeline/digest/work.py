"""AI at Work (/work): the practical card behind the section, and everything its pages are built from.

The summary model fills a "work_card" for an article only when a marketer, a small-business owner or
a small team can act on it: a tool, a feature, a how-to, or a price or policy change that affects
their work. Industry news, funding rounds and research get no card, so the section is a different
kind of output from the news digest, not a second category page.

This module holds three things, all pure functions so they can be tested without a database:

- clean_card: validate and clamp what the model returned (enrich.py), the way model_release and
  funding are cleaned. A card without a tool, a plain sentence or an honest caveat is dropped: the
  section promises "watch out" on every card, so a card that cannot keep that promise is no card.
- the section's derived facts: which job a card belongs to, how useful it is, whether it is
  something to skip this week, and one row per tool for the directory (export.py).
- the section's own briefing: the most useful practical items of the last day or two, ordered by
  usefulness rather than news importance.
"""
from __future__ import annotations

import re
from datetime import timedelta

from .trackers import _plain, org_key

# Who a card is for, as the model may answer.
WHO = ("marketer", "sales", "founder", "support", "ops", "ecommerce")
WHO_LABELS = {
    "marketer": "marketers",
    "sales": "sales",
    "founder": "founders",
    "support": "support teams",
    "ops": "operations",
    "ecommerce": "online shops",
}
# How much work it is before it does anything useful.
EFFORTS = ("minutes", "an afternoon", "needs a developer")
# The jobs the section's filters offer, in the order they are shown.
JOBS = {
    "customers": "Get customers",
    "content": "Make content",
    "sell": "Sell",
    "support": "Support customers",
    "business": "Run the business",
}
WHO_JOBS = {
    "marketer": ["customers"],
    "sales": ["sell"],
    "ecommerce": ["sell"],
    "support": ["support"],
    "founder": ["business"],
    "ops": ["business"],
}
# "Make content" is not a reader, it is a task: it comes from what the card says the tool does.
CONTENT_WORDS = re.compile(
    r"\b(content|copy(?:writing)?|writ(?:e|es|ing)|draft(?:s|ing)?|blog|article|newsletter|caption|"
    r"headline|script|video|image|photo|design|graphic|slide|podcast|voice ?over|social (?:post|media)|"
    r"seo|transcri(?:be|pt)|translat)\w*", re.I)
COST_KINDS = ("free", "free tier", "included", "paid", "unknown")
# Wording in a caveat that means "not for everyone yet": the weekly playbook lists these as things
# to skip for now rather than things to try.
NOT_YET = re.compile(
    r"\b(wait ?list|beta|preview|early access|invite[- ]only|pilot|limited (?:to|availability|rollout|release)|"
    r"rolling out|not (?:yet )?(?:available|live)|coming (?:soon|later)|deprecat|sunset|us[- ]only|"
    r"enterprise[- ](?:only|plan|tier|customers))\w*", re.I)


def _text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _list(value) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def cost_kind(cost: str) -> str:
    """What the reader actually has to pay, from the model's own words."""
    text = (cost or "").strip().lower()
    if not text:
        return "unknown"
    if "included" in text or "already" in text:
        return "included"
    if "free tier" in text or "freemium" in text or "free plan" in text:
        return "free tier"
    if re.search(r"\$|\beur\b|€|£|paid|subscription|per month|/mo|per seat|credits", text):
        return "paid"
    if "free" in text:
        return "free"
    return "unknown"


def _effort(value) -> str | None:
    """Three answers only; anything else is read for its meaning, then dropped if it has none."""
    text = (str(value or "")).strip().lower()
    if not text:
        return None
    if text in EFFORTS:
        return text
    if re.search(r"develop|engineer|\bapi\b|\bcode\b|technical", text):
        return "needs a developer"
    if re.search(r"minute|instant|straight away|right away|no setup|sign ?up", text):
        return "minutes"
    if re.search(r"afternoon|hour|a day|half a day|weekend|evening", text):
        return "an afternoon"
    return None


def clean_card(value) -> dict | None:
    """The card as it is stored, or None when the article does not belong in the section.

    Dropped when the model says it does not fit, when it names no tool, when it cannot say in one
    sentence what the thing does, or when it has no honest caveat. Everything else is clamped, so a
    talkative or a sloppy answer cannot reach a page.
    """
    if not isinstance(value, dict):
        return None
    if not value.get("fits"):
        return None
    tool = _text(value.get("tool"), 120)
    what = _text(value.get("what_it_does"), 220)
    watch = _text(value.get("watch_out"), 260)
    if not tool or len(what) < 15 or len(watch) < 10:
        return None
    who, seen = [], set()
    for w in _list(value.get("who_for")):
        w = str(w).strip().lower()
        if w in WHO and w not in seen:
            seen.add(w)
            who.append(w)
    if not who:
        who = ["marketer"]
    uses = []
    for u in _list(value.get("use_for")):
        u = _text(u, 90)
        if u and u.lower() not in {x.lower() for x in uses}:
            uses.append(u)
    uses = uses[:3]
    if not uses:
        return None
    link = _text(value.get("link"), 500)
    if not link.startswith(("http://", "https://")):
        link = ""
    return {
        "fits": True,
        "tool": tool,
        "maker": _text(value.get("maker"), 120) or None,
        "what_it_does": what,
        "who_for": who[:4],
        "use_for": uses,
        "cost": _text(value.get("cost"), 60) or "unknown",
        "effort": _effort(value.get("effort")),
        "watch_out": watch,
        "link": link or None,
    }


def jobs_for(card: dict) -> list[str]:
    """The jobs a card helps with: from who it is for, plus "make content" from what it does."""
    jobs: list[str] = []
    for w in card.get("who_for") or []:
        for job in WHO_JOBS.get(w, []):
            if job not in jobs:
                jobs.append(job)
    text = " ".join([card.get("what_it_does") or "", *(card.get("use_for") or [])])
    if CONTENT_WORDS.search(text) and "content" not in jobs:
        jobs.append("content")
    return [j for j in JOBS if j in jobs]


def skip_reason(card: dict) -> str | None:
    """Why a reader should not spend this week on it, in their own words, or None."""
    if card.get("effort") == "needs a developer":
        return "needs a developer"
    match = NOT_YET.search(card.get("watch_out") or "")
    if match:
        word = match.group(0).lower()
        if "wait" in word:
            return "waitlist only"
        if "us-only" in word or "us only" in word:
            return "United States only"
        if "enterprise" in word:
            return "enterprise plans only"
        if "deprecat" in word or "sunset" in word:
            return "being withdrawn"
        return "not open to everyone yet"
    return None


EFFORT_POINTS = {"minutes": 1.0, "an afternoon": 0.6, "needs a developer": 0.0}
COST_POINTS = {"free": 0.8, "free tier": 0.7, "included": 0.6, "paid": 0.3, "unknown": 0.1}


def usefulness(card: dict, story: dict | None = None) -> float:
    """How much use a card is to a small team, which is not how big the news is.

    A free thing you can try in minutes, with an official link and three concrete uses, beats a
    headline-grabbing launch that needs a developer. News importance is deliberately not part of
    this: the main briefing already ranks by that.
    """
    score = 0.0
    score += 1.0 if card.get("link") else 0.0
    score += 0.5 * min(len(card.get("use_for") or []), 3)
    score += EFFORT_POINTS.get(card.get("effort") or "", 0.2)
    score += COST_POINTS.get(cost_kind(card.get("cost") or ""), 0.1)
    score += 0.3 * min(len(card.get("who_for") or []), 3)
    score += 0.4 if card.get("maker") else 0.0
    if skip_reason(card):
        score -= 0.6
    if story:
        # A how-to is something to do today; and coverage by more than one publisher is mild support.
        if (story.get("contentType") or "") == "tutorial":
            score += 0.5
        score += min(0.6, 0.2 * max(0, (story.get("articleCount") or 1) - 1))
    return round(score, 3)


# ---------------------------------------------------------------------------- export shapes

def card_out(card: dict, story: dict | None = None) -> dict:
    """The card as the site reads it, with the facts the pages derive from it."""
    return {
        "tool": card["tool"],
        "maker": card.get("maker"),
        "whatItDoes": card["what_it_does"],
        "whoFor": card.get("who_for") or [],
        "useFor": card.get("use_for") or [],
        "cost": card.get("cost") or "unknown",
        "costKind": cost_kind(card.get("cost") or ""),
        "effort": card.get("effort"),
        "watchOut": card["watch_out"],
        "link": card.get("link"),
        "jobs": jobs_for(card),
        "skip": skip_reason(card),
        "usefulness": usefulness(card, story),
    }


def _completeness(card: dict) -> int:
    return sum(1 for k in ("maker", "link", "effort") if card.get(k)) + len(card.get("use_for") or [])


def story_card(story: dict, cards: dict[int, dict]) -> dict | None:
    """One card per story: the lead article's when it has one, else the most complete of the others.

    A story gathers several articles about the same thing; they do not all carry a card, and the
    ones that do can be more or less specific about the tool. `cards` maps article id to the stored
    card, so the answer is the card itself, not a copy of the exported shape.
    """
    mine = [(a["id"], cards[a["id"]]) for a in story.get("articles") or [] if cards.get(a["id"])]
    if not mine:
        return None
    best = max(_completeness(c) for _i, c in mine)
    lead = next((c for i, c in mine if i == story.get("leadArticleId")), None)
    if lead is not None and _completeness(lead) >= best - 1:
        return lead
    return max((c for _i, c in mine), key=_completeness)


def section_stories(stories: list[dict]) -> list[dict]:
    """The stories the section covers: any story with a card, whatever its category."""
    return [s for s in stories if s.get("workCard")]


# ---------------------------------------------------------------------------- tools directory

def tool_key(tool: str, maker: str | None) -> str:
    """Same tool, same key: "Gemini in Google Ads" and "Google Ads (Gemini)" are not the same tool,
    but "Canva Magic Studio" (Canva) and "Magic Studio" (Canva Inc.) are."""
    org = org_key(maker)
    words = [w for w in _plain(tool or "").split() if w and w != org]
    return ("".join(words).replace(".", "") or org) + "|" + org


def build_tools(stories: list[dict]) -> list[dict]:
    """One row per tool for /work/tools, newest change first.

    Spellings are merged (tool_key), the most complete fields win, and every story that mentioned
    the tool is kept so the row can show what changed and when.
    """
    rows: dict[str, dict] = {}
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or "", reverse=True):
        card = s.get("workCard")
        if not card:
            continue
        key = tool_key(card["tool"], card.get("maker"))
        date = s.get("firstPublishedAt") or s.get("updatedAt")
        row = rows.get(key)
        if row is None:
            rows[key] = {
                "key": key,
                "tool": card["tool"],
                "maker": card.get("maker"),
                "whatItDoes": card["whatItDoes"],
                "whoFor": list(card.get("whoFor") or []),
                "jobs": list(card.get("jobs") or []),
                "cost": card.get("cost"),
                "costKind": card.get("costKind"),
                "effort": card.get("effort"),
                "watchOut": card.get("watchOut"),
                "link": card.get("link"),
                "lastChange": date,
                "firstSeen": date,
                "changes": [{"slug": s["slug"], "headline": s["headline"], "date": date}],
            }
            continue
        # Later rows are older: fill what is missing, keep the earliest date as first seen.
        if len(card["tool"]) > len(row["tool"]):
            row["tool"] = card["tool"]  # the fuller spelling reads better in the directory
        for field, source in (("maker", "maker"), ("link", "link"), ("effort", "effort")):
            if not row.get(field) and card.get(source):
                row[field] = card[source]
        if not row.get("costKind") or row["costKind"] == "unknown":
            row["cost"], row["costKind"] = card.get("cost"), card.get("costKind")
        for w in card.get("whoFor") or []:
            if w not in row["whoFor"]:
                row["whoFor"].append(w)
        for j in card.get("jobs") or []:
            if j not in row["jobs"]:
                row["jobs"].append(j)
        if (date or "") < (row["firstSeen"] or "~"):
            row["firstSeen"] = date
        if len(row["changes"]) < 8:
            row["changes"].append({"slug": s["slug"], "headline": s["headline"], "date": date})
    out = sorted(rows.values(), key=lambda r: r["lastChange"] or "", reverse=True)
    for row in out:
        row["jobs"] = [j for j in JOBS if j in row["jobs"]]
        row["changeCount"] = len(row["changes"])
    return out


# ---------------------------------------------------------------------------- weekly playbook

def week_key(iso: str | None) -> str:
    """ISO week key like 2026-W38, the same one intros.py and the site use."""
    from datetime import datetime, timezone

    d = datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else datetime.now(timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weeks(stories: list[dict]) -> dict[str, dict]:
    """Every week of the section, as the playbook page shows it: what changed, what to try, what to
    skip. "Skip" is never a judgement of the tool, only of this week: a waitlist, a beta, or work
    that needs a developer."""
    out: dict[str, dict] = {}
    for s in stories:
        card = s.get("workCard")
        if not card:
            continue
        key = week_key(s.get("firstPublishedAt") or s.get("updatedAt"))
        bucket = out.setdefault(key, {"week": key, "changed": [], "try": [], "skip": []})
        bucket["changed"].append(s)
        (bucket["skip"] if card.get("skip") else bucket["try"]).append(s)
    for bucket in out.values():
        bucket["changed"].sort(key=lambda s: s.get("firstPublishedAt") or "", reverse=True)
        bucket["try"].sort(key=lambda s: -(s["workCard"]["usefulness"]))
        bucket["skip"].sort(key=lambda s: -(s["workCard"]["usefulness"]))
        bucket["tools"] = len({tool_key(s["workCard"]["tool"], s["workCard"].get("maker")) for s in bucket["changed"]})
        bucket["changed"] = [s["id"] for s in bucket["changed"]]
        bucket["try"] = [s["id"] for s in bucket["try"][:8]]
        bucket["skip"] = [s["id"] for s in bucket["skip"][:6]]
    return out


# ---------------------------------------------------------------------------- section briefing

BRIEFING_SIZE = 5
BRIEFING_ALSO = 6


def build_briefing(stories: list[dict], now) -> dict:
    """The section's own briefing: the most useful practical items of the last day or two.

    Its selection rule is not the main briefing's. That one asks "what is the biggest news"; this
    one asks "what can a small team do this week", and orders by usefulness (work.usefulness), so a
    free tool you can try in minutes leads over a larger launch that needs a developer. Overlap
    between the two briefings is allowed on purpose: the same launch is news on the front page and
    a thing to try here, and the two pages say different things about it.
    """
    section = section_stories(stories)

    def since(hours: int) -> list[dict]:
        cutoff = (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        return [s for s in section if (s.get("firstPublishedAt") or "") >= cutoff]

    fresh, window = since(24), 24
    if len(fresh) < BRIEFING_SIZE:
        # A quiet day reaches back a second day, but only when that actually finds something, so
        # the stated window is always the window the list came from.
        wider = since(48)
        if len(wider) > len(fresh):
            fresh, window = wider, 48
    fresh.sort(key=lambda s: (-(s["workCard"]["usefulness"]), s.get("firstPublishedAt") or ""))
    top = fresh[:BRIEFING_SIZE]
    also = fresh[BRIEFING_SIZE : BRIEFING_SIZE + BRIEFING_ALSO]
    tools = {tool_key(s["workCard"]["tool"], s["workCard"].get("maker")) for s in top + also}
    free = sum(1 for s in top + also if s["workCard"]["costKind"] in ("free", "free tier", "included"))
    return {
        "date": now.date().isoformat(),
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "windowHours": window,
        "storyIds": [s["id"] for s in top],
        "alsoIds": [s["id"] for s in also],
        "stats": {
            "items": len(fresh),
            "tools": len(tools),
            "free": free,
            "minutes": max(2, round(sum(len(s["workCard"]["useFor"]) + 3 for s in top) / 4)),
        },
    }
