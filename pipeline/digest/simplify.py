"""Step: rewrite recent AI at Work cards in plain words (plain.py) with a free model.

Cards written before the plain-words prompt are long and wordy: "Allows advertisers to connect
conversational AI agents to ChatGPT ads and manage campaigns via Shopify or HubSpot". The export
already cuts them back by rules alone (work.plain_card), but rules can only shorten and swap; they
cannot write "Answer shoppers' questions inside ChatGPT ads". This step asks a model to rewrite the
five fields a reader sees first (headline, you_get, what_it_does, use_for, watch_out) from the card's
own fields and the story's stored summary, and keeps each rewritten field only when:

- it keeps the same rules every card keeps (plain.py: length, verb-first headline, jargon, reading
  level), and
- it is grounded: no figure and no name that the card and the summary do not already have
  (checks.unsupported_figures, checks.unsupported_names), and a catch that still says the same about
  who cannot use it (the same "leave for now" reason and region and plan limits).

A field that fails keeps its current text. The card is then marked "simplified", so it is never
asked about again, whether or not anything changed.

What holds it in check (the same shape as howto.py):
- it runs after the export (run.ORDER), reads the export the run just wrote (site/src/data/stories.json
  and work-briefing.json: no database reads to choose cards), and writes back through the runner's
  copy of the rows (cache.py), only for the cards it changed;
- only cards of the last WINDOW_DAYS days, those on the hub and the home page first (the featured
  pick, the briefing, then newest first);
- at most MAX_CARDS cards a run, BATCH cards per model call, inside TIME_BUDGET_SECONDS; the free
  providers in the usual order (howto.ask_model: Gemini, Groq, Ollama Cloud);
- a batch no model answered is tried again after RETRY_HOURS (runner cache), never in a loop.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import checks, config, plain, work

log = logging.getLogger("digest.simplify")

MAX_CARDS = 10
BATCH = 5
TIME_BUDGET_SECONDS = 90
WINDOW_DAYS = 14
RETRY_HOURS = 12
SUMMARY_WORDS = 220            # of the story summary sent with each card
FIELDS = ("headline", "you_get", "what_it_does", "use_for", "watch_out")
CACHE_FILE = config.CACHE_DIR / "simplify.json.gz"

PROMPT = """Rewrite these cards about AI tools for a busy small-business owner who is not technical and wants to decide in seconds whether to try one.
For each card return its "id" and these fields:
- "headline": {headline}
- "you_get": {you_get}
- "what_it_does": {what_it_does}
- "use_for": {use_for}
- "watch_out": {watch_out}
{style} Gain framing: say what the owner gets. Use only facts in the card and its summary: no new numbers, names, prices or claims, and keep the catch's meaning (the same region, plan or waitlist).
Return ONLY a JSON object: {{"cards": [{{"id": ..., "headline": ..., "you_get": ..., "what_it_does": ..., "use_for": [...], "watch_out": ...}}]}}

Cards:
{cards}
"""


# ---------------------------------------------------------------------------- which cards

def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def candidates(stories: list[dict], briefing: dict | None, now: datetime) -> list[tuple[dict, dict, list[int]]]:
    """(story, card, article ids) for exported cards of the last WINDOW_DAYS days not yet simplified:
    the featured pick first, then the section briefing, then newest first. The article ids are those
    whose own card is the story's (same tool and link), as howto.py finds them."""
    cutoff = now - timedelta(days=WINDOW_DAYS)
    b = briefing or {}
    first = [i for i in [b.get("featuredId"), *(b.get("storyIds") or []), *(b.get("alsoIds") or [])] if i is not None]
    rank = {sid: n for n, sid in enumerate(dict.fromkeys(first))}
    pool = []
    for s in stories:
        card = s.get("workCard")
        when = _parse(s.get("firstPublishedAt") or s.get("updatedAt"))
        if not card or card.get("simplified") or when is None or when < cutoff:
            continue
        ids = [a["id"] for a in s.get("articles") or []
               if (a.get("workCard") or {}).get("tool") == card["tool"] and (a.get("workCard") or {}).get("link") == card.get("link")]
        if ids:
            pool.append((s, card, ids))
    pool.sort(key=lambda t: (rank.get(t[0]["id"], len(rank)), -(_parse(t[0].get("firstPublishedAt") or t[0].get("updatedAt")).timestamp())))
    return pool


def _summary(story: dict) -> str:
    text = " ".join(filter(None, [story.get("summaryMd") or "", " ".join(story.get("keyPoints") or [])]))
    text = re.sub(r"[*_#>`]+", "", text)
    return " ".join(text.split()[:SUMMARY_WORDS])


def source_of(story: dict, card: dict) -> str:
    """What a rewrite may draw on: every field of the card and the story's summary and key points."""
    parts = [card.get("tool"), card.get("maker"), card.get("headline"), card.get("youGet"), card.get("whatItDoes"),
             " ".join(card.get("useFor") or []), card.get("watchOut"), card.get("cost"), card.get("includedIn"),
             " ".join(card.get("steps") or []), card.get("prompt"), _summary(story)]
    return " ".join(p for p in parts if p)


def card_payload(story: dict, card: dict) -> dict:
    return {
        "id": story["id"], "tool": card["tool"], "maker": card.get("maker"), "headline": card.get("headline"),
        "you_get": card.get("youGet"), "what_it_does": card.get("whatItDoes"), "use_for": card.get("useFor") or [],
        "watch_out": card.get("watchOut"), "cost": card.get("cost"), "summary": _summary(story),
    }


def build_prompt(batch: list[tuple[dict, dict, list[int]]]) -> str:
    g = plain.prompt_guide()
    cards = json.dumps([card_payload(s, c) for s, c, _ids in batch], ensure_ascii=False, indent=0)
    return PROMPT.format(cards=cards, **{k: g[k] for k in ("headline", "you_get", "what_it_does", "use_for", "watch_out", "style")})


# ---------------------------------------------------------------------------- keeping a rewrite

def _grounded(text: str, source: str) -> bool:
    """No figure and no name the card and its summary do not already have."""
    if not text:
        return False
    if checks.unsupported_figures(text, source):
        return False
    return not checks.unsupported_names(work.you_get_names(text), text, source)


def rewrite(stored: dict, answer: dict, source: str) -> tuple[dict, list[str]]:
    """The stored card with each rewritten field that keeps the rules and is grounded, and the names
    of the fields that changed. `stored` is the card as the database holds it (work.py field names)."""
    card = dict(stored)
    tool = card.get("tool") or ""
    names = [n for n in (tool, card.get("maker")) if n]
    changed: list[str] = []
    answer = answer if isinstance(answer, dict) else {}

    uses = plain.plain_uses(work._list(answer.get("use_for")), names)
    if len(uses) >= 2 and all(_grounded(u, source) for u in uses):
        card["use_for"] = uses
        changed.append("use_for")

    what = plain.plain_what(work._text(answer.get("what_it_does"), 400), names)
    if what and _grounded(what, source):
        card["what_it_does"] = what
        changed.append("what_it_does")

    new_watch = work._text(answer.get("watch_out"), 400).rstrip(".")
    if new_watch:
        keep = work._keeps_limits(stored)
        line = plain.plain_watch_out(new_watch, names, keep=keep)
        if line != plain.GENERIC_CATCH and _grounded(line, source) and keep(line):
            card["watch_out"] = line
            changed.append("watch_out")

    headline = plain.plain_headline(work._text(answer.get("headline"), 200), names, tool)
    if headline and _grounded(headline, source):
        card["headline"] = headline
        changed.append("headline")

    line = plain.plain_you_get(work._text(answer.get("you_get"), 400), names)
    if line and _grounded(line, source) and plain.overlap(line, card.get("headline") or "") < plain.OVERLAP_MAX:
        card["you_get"] = line
        changed.append("you_get")

    card["simplified"] = True
    return card, changed


# ---------------------------------------------------------------------------- the cache

def load_cache() -> dict:
    try:
        with gzip.open(CACHE_FILE, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, EOFError):
        return {}


def save_cache(data: dict, now: float) -> None:
    data = {k: v for k, v in data.items() if now - float(v or 0) < 7 * 86400}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(CACHE_FILE, "wt", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:
        log.warning("simplify: cache not saved (%s)", exc)


# ---------------------------------------------------------------------------- store

def store_cards(updates: dict[int, dict]) -> int:
    """Write the rewritten fields onto the stored cards of these articles. Each update carries only the
    five fields and the mark; the rest of the stored card (steps, prompt, link...) is left as it is.
    The cards are read from the runner's copy of the rows (cache.py), which the export just refreshed."""
    from sqlalchemy import update

    from . import cache, db

    written = 0
    eng = db.engine()
    with eng.begin() as conn:
        mirror = cache.articles(conn)
        rows = [mirror[i] for i in updates if i in mirror]
        texts = cache.article_text(conn, rows)
        for i, fields in updates.items():
            t = texts.get(i)
            card = dict(t.work_card) if t is not None and isinstance(t.work_card, dict) else None
            if not card or card.get("simplified"):
                continue
            card.update(fields)
            conn.execute(update(db.articles).where(db.articles.c.id == i).values(work_card=card))
            written += 1
        cache.forget_details(conn, cache.ARTICLE_TEXT, list(updates))
    return written


def _stored_shape(card: dict) -> dict:
    """The exported card (work.card_out) in the stored names the rules read."""
    return {"fits": True, "tool": card["tool"], "maker": card.get("maker"), "headline": card.get("headline") or "",
            "you_get": card.get("youGet") or "", "what_it_does": card.get("whatItDoes") or "",
            "use_for": list(card.get("useFor") or []), "watch_out": card.get("watchOut") or "",
            "who_for": list(card.get("whoFor") or []), "cost": card.get("cost") or "", "effort": card.get("effort"),
            "link": card.get("link"), "included_in": card.get("includedIn") or ""}


# ---------------------------------------------------------------------------- the step

def run(stories: list[dict] | None = None, briefing: dict | None = None, ask=None, store=store_cards,
        now: datetime | None = None, limit: int = MAX_CARDS, batch_size: int = BATCH,
        budget_seconds: float = TIME_BUDGET_SECONDS, cache_data: dict | None = None) -> dict:
    stats = {"candidates": 0, "asked": 0, "answered": 0, "failed": 0, "cards": 0, "fields": 0, "written": 0}
    if stories is None:
        try:
            stories = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {**stats, "skipped": "no export"}
        try:
            briefing = json.loads((config.SITE_DATA_DIR / "work-briefing.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            briefing = None
    if ask is None:
        from .howto import ask_model as ask
    now = now or datetime.now(timezone.utc)
    stamp = now.timestamp()
    own_cache = cache_data is None
    tried = load_cache() if own_cache else cache_data
    todo = [t for t in candidates(stories, briefing, now)
            if stamp - float(tried.get(str(t[0]["id"])) or 0) > RETRY_HOURS * 3600]
    stats["candidates"] = len(todo)
    todo = todo[:limit]
    t0 = time.time()
    updates: dict[int, dict] = {}
    for start in range(0, len(todo), batch_size):
        if time.time() - t0 > budget_seconds:
            break
        batch = todo[start:start + batch_size]
        stats["asked"] += len(batch)
        try:
            answer = ask(build_prompt(batch))
        except Exception as exc:  # noqa: BLE001
            log.info("simplify: no answer (%s)", exc)
            answer = None
        got = {}
        for item in ((answer or {}).get("cards") or []) if isinstance(answer, dict) else []:
            if isinstance(item, dict):
                try:
                    got[int(item.get("id"))] = item
                except (TypeError, ValueError):
                    continue
        for story, card, ids in batch:
            item = got.get(story["id"])
            if item is None:
                stats["failed"] += 1
                tried[str(story["id"])] = stamp  # asked again after RETRY_HOURS, not every run
                continue
            stats["answered"] += 1
            stored = _stored_shape(card)
            new, changed = rewrite(stored, item, source_of(story, card))
            stats["cards"] += 1
            stats["fields"] += len(changed)
            fields = {f: new[f] for f in changed}
            fields["simplified"] = True
            for i in ids:
                updates[i] = fields
    if own_cache:
        save_cache(tried, stamp)
    if updates:
        try:
            stats["written"] = store(updates)
        except Exception as exc:  # noqa: BLE001
            log.warning("simplify: cards not stored (%s)", exc)
            stats["storeFailed"] = True
    return stats
