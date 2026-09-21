"""Share today: the day's few stories worth pushing to LinkedIn and X by hand, with posts ready to paste.

LinkedIn and X are not connected to the pipeline and stay manual: the owner posts from his own
logged-in accounts, from the admin page (Today tab). This module only picks the stories and writes the
text. It reads files the export step already wrote (stories.json, briefing.json, work-briefing.json)
and pipeline/digest/shared.json, the record of what was already shared, which the admin page commits
through the GitHub API the same way it edits moderation.yaml. No database read.

The shortlist, 5 to 8 items:
1. the briefing's top stories, confirmed ones first (two or more publishers, the lab's own post, or a
   pin), up to four;
2. the AI at Work featured pick (the marketing focus), else the section's first item;
3. one strong story from a category not yet on the list, for variety;
4. filled up to five from the rest of the briefing and the day's best by score.
A story shared on an earlier day (either network) drops off; one shared today stays, marked done.

The posts are rules only, from the story's own headline, key points and "why it matters": nothing is
added that the story does not say, hype words are swapped out (checks.py), and the headline goes
through checks.discipline_headline. The X text fits in 280 characters counting the link as 23 (t.co).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from . import checks, config, hold, images

SHARED_FILE = Path(__file__).with_name("shared.json")
NETWORKS = ("linkedin", "x")
X_LIMIT = 280
TCO_LENGTH = 23  # X wraps every link in t.co and counts it as 23 characters whatever its length
MIN_ITEMS, MAX_ITEMS = 5, 8
BRIEFING_TAKE = 4
FRESH_HOURS = 96  # the briefing's widest window; nothing older is offered
CAMPAIGN = "share-today"
SOURCE_IMAGES = 3  # source pictures offered per item in the admin (download by hand, never auto-posted)

# One tag for the category, then the story's main company when it is a single word. At most three on
# LinkedIn and two on X: more reads as stuffing and does nothing for reach.
CATEGORY_TAGS = {
    "models": "GenerativeAI", "marketing": "Marketing", "agents": "AIAgents", "research": "AIResearch",
    "business": "Startups", "policy": "AIPolicy", "hardware": "Semiconductors", "enterprise": "EnterpriseAI",
    "robotics": "Robotics", "society": "FutureOfWork",
}
# Words that make a post read like an advert. The first ones are swapped by checks' own rules; these
# are the ones that are simply dropped from a line (with the article fixed) or refused in tests.
HYPE_WORDS = ("revolutionary", "revolutionize", "game-changer", "game changer", "game-changing", "groundbreaking",
              "ground-breaking", "unleash", "mind-blowing", "jaw-dropping", "earth-shattering", "mind-bending")


# ---------------------------------------------------------------------------- the done-state file

def load_shared(path: Path | None = None) -> dict:
    """shared.json: {"stories": {"<id>": {"slug": ..., "linkedin": {"at": iso, "url": ...}, "x": {...}}}}.
    A missing or broken file is an empty record, never an error."""
    try:
        data = json.loads((path or SHARED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stories": {}}
    stories = data.get("stories") if isinstance(data, dict) else None
    return {"stories": stories if isinstance(stories, dict) else {}}


def _records(shared: dict):
    """(story id, network, at, url) for every share on record."""
    for sid, rec in (shared.get("stories") or {}).items():
        if not isinstance(rec, dict):
            continue
        for net in NETWORKS:
            r = rec.get(net)
            if isinstance(r, dict) and r.get("at"):
                try:
                    yield int(sid), net, str(r["at"]), r.get("url") or ""
                except (TypeError, ValueError):
                    continue


def shared_before(shared: dict, day: str) -> set[int]:
    """Stories shared on any network before `day` (YYYY-MM-DD, UTC): they drop off the shortlist."""
    return {sid for sid, _, at, _ in _records(shared) if at[:10] < day}


def done_for(shared: dict, sid: int) -> dict:
    rec = (shared.get("stories") or {}).get(str(sid))
    if not isinstance(rec, dict):
        return {}
    return {net: {"at": rec[net].get("at"), "url": rec[net].get("url") or ""} for net in NETWORKS
            if isinstance(rec.get(net), dict) and rec[net].get("at")}


def week_counts(shared: dict, now: datetime) -> dict:
    cutoff = (now - timedelta(days=7)).isoformat()[:19]
    out = {net: 0 for net in NETWORKS}
    for _, net, at, _ in _records(shared):
        if at[:19] >= cutoff:
            out[net] += 1
    return out


# ---------------------------------------------------------------------------- the shortlist

def publishers(story: dict) -> int:
    return len({hold.registrable(a.get("domain") or "") for a in story.get("articles") or []} - {""})


def confirmed(story: dict) -> bool:
    """The export's rule (export.build_briefing): two publishers, the lab's own post, or a pin."""
    return bool(story.get("pinned") or story.get("hasPrimary") or publishers(story) >= 2)


def _backing(story: dict) -> str:
    n = publishers(story)
    if n >= 2:
        return f"{n} publishers"
    if story.get("hasPrimary"):
        return "the lab's own post"
    return "single outlet"


def shortlist(stories: list[dict], briefing: dict | None, work_briefing: dict | None, shared: dict, now: datetime) -> list[dict]:
    """[{"story": ..., "kind": briefing|work|variety|fill, "reason": "Led the briefing · 4 publishers"}]."""
    by_id = {s["id"]: s for s in stories}
    gone = shared_before(shared, now.date().isoformat())
    cutoff = (now - timedelta(hours=FRESH_HOURS)).isoformat()[:19]
    fresh = lambda s: (s.get("firstPublishedAt") or "")[:19] >= cutoff or s.get("pinned")  # noqa: E731
    # The briefing and AI at Work lists are today's by construction (a developing story may be older);
    # the rest of the pool is limited to the briefing's widest window.
    ok = lambda s: s is not None and s["id"] not in gone  # noqa: E731
    picked: list[dict] = []
    taken: set[int] = set()

    def add(story: dict, kind: str, reason: str) -> None:
        picked.append({"story": story, "kind": kind, "reason": reason})
        taken.add(story["id"])

    # 1. The briefing's top stories, confirmed first, in briefing order.
    top_ids = list((briefing or {}).get("storyIds") or [])
    top = [(i, by_id.get(sid)) for i, sid in enumerate(top_ids)]
    top = [(i, s) for i, s in top if ok(s)]
    ordered = [t for t in top if confirmed(t[1])] + [t for t in top if not confirmed(t[1])]
    for i, s in ordered[:BRIEFING_TAKE]:
        add(s, "briefing", f"{'Led the briefing' if i == 0 else f'No. {i + 1} in the briefing'} · {_backing(s)}")

    # 2. The AI at Work featured pick, else the section's first item.
    wb = work_briefing or {}
    work_ids = ([wb["featuredId"]] if wb.get("featuredId") else []) + list(wb.get("storyIds") or [])
    for sid in work_ids:
        s = by_id.get(sid)
        if ok(s) and sid not in taken:
            label = "AI at Work pick" if sid == wb.get("featuredId") else "Top of AI at Work"
            card = s.get("workCard") or {}
            cost = card.get("costKind") if card.get("costKind") not in (None, "unknown") else None
            add(s, "work", " · ".join(x for x in (label, "for marketers", cost) if x))
            break

    # Everything else eligible today, best first: the rest of the briefing, then fresh stories by score.
    also = [by_id.get(sid) for sid in (briefing or {}).get("alsoIds") or []]
    rest = [s for s in also if ok(s) and s["id"] not in taken]
    in_also = {s["id"] for s in rest}
    rest += sorted((s for s in stories if ok(s) and fresh(s) and s["id"] not in taken and s["id"] not in in_also),
                   key=lambda s: -(s.get("score") or 0))

    # 3. One strong story from a category not on the list yet.
    cats = {p["story"].get("category") for p in picked}
    other = [s for s in rest if picked and s.get("category") not in cats and (s.get("importance") or 0) >= 6]
    if other:
        s = max(other, key=lambda s: ((s.get("importance") or 0), s.get("score") or 0))
        add(s, "variety", f"Variety: {s.get('categoryName') or s.get('category')} · importance {s.get('importance')}/10 · {_backing(s)}")

    # 4. Fill to the minimum from what is left.
    for s in rest:
        if len(picked) >= MIN_ITEMS:
            break
        if s["id"] not in taken:
            where = "Also in the briefing" if s["id"] in in_also else "Top by score today"
            add(s, "fill", f"{where} · {_backing(s)}")
    return picked[:MAX_ITEMS]


# ---------------------------------------------------------------------------- the posts

def story_url(slug: str, network: str | None = None) -> str:
    url = f"{config.SITE_URL}/story/{quote(slug)}"
    if network:
        url += f"?utm_source={network}&utm_medium=social&utm_campaign={CAMPAIGN}"
    return url


def x_length(text: str) -> int:
    """Length as X counts it: every link is 23 (t.co), and characters outside the Latin, punctuation
    and general-symbol ranges (CJK, most emoji) count twice (twitter-text's weighted length)."""
    n = 0
    for part in re.split(r"(https?://\S+)", text):
        if re.match(r"https?://", part):
            n += TCO_LENGTH
            continue
        for ch in part:
            c = ord(ch)
            light = c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247
            n += 1 if light else 2
    return n


def calm(text: str | None) -> str:
    """Hype words out: checks' swaps ("unleashes" -> "releases"), the empty adjectives dropped."""
    t = re.sub(r"\s+", " ", text or "").strip()
    for pattern, repl in checks._HYPE:
        t = pattern.sub(repl, t)
    t = checks._HYPE_ADJ.sub(checks._drop_adjective, t)
    t = re.sub(r"!+", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-–—") + "…"


def _sentence(text: str | None, limit: int = 220) -> str:
    """The first sentence, plain text, clipped at a word."""
    plain = re.sub(r"[*_`#>\[\]]", "", text or "")
    plain = re.sub(r"\(https?://[^)]*\)", "", plain).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", plain)
    return _clip(calm(m.group(1) if m else plain), limit)


def _stop(text: str) -> str:
    text = text.rstrip()
    return text if not text or text[-1] in ".?!…:" else text + "."


def headline(story: dict) -> str:
    lead = next((a for a in story.get("articles") or [] if a.get("isLead")), None) or (story.get("articles") or [{}])[0]
    fixed, _ = checks.discipline_headline(story.get("headline"), lead.get("title"), checks.entity_names(story.get("entities")))
    return calm(fixed)


def hashtags(story: dict, limit: int) -> list[str]:
    tags = []
    if story.get("workCard") or story.get("category") == "marketing":
        tags += ["Marketing", "SmallBusiness"]
    else:
        tag = CATEGORY_TAGS.get(story.get("category") or "")
        if tag:
            tags.append(tag)
    for name in ((story.get("entities") or {}).get("companies") or [])[:1]:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,15}", str(name)):
            tags.append(str(name))
    if not any("AI" in t for t in tags):
        tags.append("AI")  # last, so it is the one left out when there is room for two
    out: list[str] = []
    for t in tags:
        if t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out[:limit]


def _hook(story: dict) -> str:
    head = headline(story)
    return f"For marketers and small teams: {head}" if story.get("workCard") else head


def _substance(story: dict) -> list[str]:
    """Two short lines the story itself says, enough to be worth reading and to make the click worth it:
    for a work item what the tool does and what it costs; otherwise two key points. Why it matters and
    how to set it up stay on the site, and the call to action says so."""
    card = story.get("workCard")
    if card:
        lines = [_sentence(card.get("youGet") or card.get("whatItDoes"), 200)]
        if card.get("cost") and card.get("costKind") != "unknown":
            lines.append(f"Cost: {_stop(_clip(calm(card['cost']), 120))}")
        return [_stop(l) for l in lines if l]
    return [_stop(_sentence(p, 200)) for p in (story.get("keyPoints") or [])[:2] if p]


def call_to_action(story: dict) -> str:
    """Why to click, in the reader's terms: what is waiting on the page, never how it was made."""
    card = story.get("workCard")
    if card and card.get("steps"):
        return "How to set it up, step by step:"
    if card:
        return "What it does for a small team, and the catch:"
    if story.get("whyItMatters"):
        return "Why it matters, and what happens next:"
    return "The full story:"


def linkedin_post(story: dict) -> str:
    link = story_url(story["slug"], "linkedin")
    body = "\n".join(l if l.startswith("Cost") else f"• {l}" for l in _substance(story))
    tags = " ".join(f"#{t}" for t in hashtags(story, 3))
    return f"{_stop(_hook(story))}\n\n{body}\n\n{call_to_action(story)} {link}\n\n{tags}".strip()


def x_post(story: dict) -> str:
    """Headline, one line of substance if it fits, up to two tags, the link last. At most 280 as X counts."""
    link = story_url(story["slug"], "x")
    tags = " ".join(f"#{t}" for t in hashtags(story, 2))
    hook = _stop(_hook(story))
    tail = f"\n\n{tags}\n{link}" if tags else f"\n\n{link}"
    card = story.get("workCard")
    room = X_LIMIT - x_length(hook + tail) - 2  # the blank line between hook and detail
    if room < 0:  # a very long headline: shorten it, no detail
        while x_length(hook + tail) > X_LIMIT:
            hook = _clip(hook, len(hook) - 5)
        return hook + tail
    # The first whole sentence that fits: why it matters, then the key points. Never cut mid-sentence;
    # when none fits, the headline stands alone.
    # A key point, not the why: the why is what the click is for.
    options = [card.get("youGet"), card.get("whatItDoes")] if card else [*(story.get("keyPoints") or []), story.get("whyItMatters")]
    for option in options:
        detail = _stop(_sentence(option, 400)) if option else ""
        if detail and not detail.endswith("…") and x_length(detail) <= room:
            return f"{hook}\n\n{detail}{tail}"
    return hook + tail


def source_images(story: dict, limit: int = SOURCE_IMAGES) -> list[dict]:
    """The pictures the story's sources declare, for the admin's "Source image" control: the company's
    own announcement first, then the lead, then the rest, one per URL, https only (the admin page is https).
    {"url", "outlet", "domain", "articleUrl", "primary"}: `primary` is True only for the company's own
    announcement (images.company_announcement), whose picture is published for sharing; any other is a
    news outlet's photo, usually licensed, that the owner must check before reusing. Nothing here is
    posted automatically."""
    arts = sorted(story.get("articles") or [],
                  key=lambda a: (not images.company_announcement(a), not a.get("isLead")))
    out, seen = [], set()
    for a in arts:
        url = (a.get("imageUrl") or "").strip()
        if not url.lower().startswith("https://") or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "outlet": a.get("source") or a.get("domain") or "the publisher",
                    "domain": a.get("domain") or "", "articleUrl": a.get("url") or "",
                    "primary": images.company_announcement(a)})
        if len(out) >= limit:
            break
    return out


def build(stories: list[dict], briefing: dict | None, work_briefing: dict | None, shared: dict, now: datetime) -> dict:
    """What admin.json carries for the Share today card."""
    items = []
    for p in shortlist(stories, briefing, work_briefing, shared, now):
        s = p["story"]
        x = x_post(s)
        items.append({
            "id": s["id"], "slug": s["slug"], "headline": s["headline"], "category": s.get("category"),
            "categoryName": s.get("categoryName") or config.CATEGORIES.get(s.get("category") or "", "AI"),
            "kind": p["kind"], "reason": p["reason"], "publishers": publishers(s),
            "url": story_url(s["slug"]),
            "links": {net: story_url(s["slug"], net) for net in NETWORKS},
            "linkedin": linkedin_post(s), "x": x, "xLength": x_length(x),
            "done": done_for(shared, s["id"]),
            "sourceImages": source_images(s),
        })
    return {"day": now.date().isoformat(), "items": items, "week": week_counts(shared, now),
            "file": "pipeline/digest/shared.json"}


def run_from_files(now: datetime, data_dir: Path | None = None, shared_path: Path | None = None) -> dict:
    """build() over the files the export step wrote."""
    d = data_dir or config.SITE_DATA_DIR

    def read(name: str, fallback):
        try:
            return json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fallback

    return build(read("stories.json", []), read("briefing.json", {}), read("work-briefing.json", {}),
                 load_shared(shared_path), now)
