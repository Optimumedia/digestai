"""What AI at Work's tools cost, kept as a history rather than thrown away every run.

A card's price used to live only in the free-text "cost" line of that week's card. When the week
rolled over, the figure went with it: the section could say "$19 a month" today and had no way to
say that it was $12 in August, or that the free tier had just been cut. More than half the cards
said "Price not stated" at all.

This module keeps one row per tool per observation:

    {tool_key, at, plan, amount, currency, period, free_limit, source_url, story_slug, quoted}

Where it lives, and why. The rows are small (a few hundred bytes each, a few thousand rows a year)
and every one of them was already read from the database as part of the card, so a table would add
egress to every run for data the run already has; the free plan's 5 GB a month is the section's
tightest budget. So the history is a file, handled the way work.json and media.json are:

- pipeline/data/cache/work-prices.json.gz is the durable copy. It rides the Actions read cache the
  workflow keeps between runs (.github/workflows/pipeline.yml, "Restore read cache"), which is what
  cache.py, howto.py and media.py already depend on.
- site/src/data/work-prices.json is the same history in the shape the site reads, rewritten on every
  export, so the pages are built from it with no database read at all.
- Losing the cache costs history, never correctness: the next run writes today's prices again, and
  the pages show the tools they can compare.

Two things fill it:

1. Every export, from the cards themselves (record_from_stories): the price the article stated, with
   the sentence that stated it, already checked against the article text (work.price_grounded).
2. The `prices` step, from the maker's own pricing page, for tools that have an official link and no
   price (run). It follows howto.py exactly: bounded per run, cached by address, free providers
   only, tolerant of every failure, and never a forum, a code host or a news site.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import checks, config, work

log = logging.getLogger("digest.prices")

FORMAT = 1
HISTORY_FILE = config.CACHE_DIR / "work-prices.json.gz"
EXPORT_FILE = config.SITE_DATA_DIR / "work-prices.json"
PAGE_CACHE_FILE = config.CACHE_DIR / "pricing-pages.json.gz"

MAX_FETCHES = 5               # maker pricing pages (and model calls) per run
TIME_BUDGET_SECONDS = 60
PAGE_WORDS = 2000             # page text sent to the model
RETRY_DAYS = 7                # a page that failed to load is tried again after this long
NONE_DAYS = 30                # a page that stated no price is asked again after this long
RECHECK_DAYS = 45             # a page that gave a price is read again after this long
OBSERVATIONS_MAX = 24         # rows kept per tool: two years of quarterly changes
KEEP_DAYS = 730               # a tool nobody has mentioned in two years leaves the history
CHANGES_MAX = 60              # tools listed on /work/prices
# A page that says a price is a page a reader can check. The paths are tried in this order.
PRICING_PATHS = ("/pricing", "/plans", "/pricing/plans", "/price", "/pricing-plans", "/plans-and-pricing")

PROMPT = """Below is the text of {maker}'s own pricing page for {tool}.
Return ONLY a JSON object: {{"price": {{"plan": the cheapest paid plan's name or "", "amount": that plan's number only (no currency sign), "currency": "USD", "EUR" or "GBP", "period": "month", "year", "one-off" or "usage", "free_limit": what the free plan gives in a few words, or "", "quoted": the sentence on this page that states the price}}}}.
Use only figures this page states, written exactly as it writes them. Do not convert a currency, do not add up, do not fill anything in from your own knowledge. If this page states no price, return {{"price": null}}.

Page text:
\"\"\"
{text}
\"\"\"
"""


# ---------------------------------------------------------------------------- the store

def _empty_store() -> dict:
    return {"format": FORMAT, "tools": {}}


def load(path=None) -> dict:
    path = path or HISTORY_FILE
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return _empty_store()
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("tools"), dict):
        return _empty_store()
    return data


def save(store: dict, now: datetime | None = None, path=None) -> None:
    """Saved with the tools seen inside KEEP_DAYS. Any failure leaves the history as it was: the
    section shows today's prices and loses only the comparison."""
    path = path or HISTORY_FILE
    store = prune(store, now or datetime.now(timezone.utc))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(store, f)
    except OSError as exc:  # noqa: BLE001
        log.warning("prices: history not saved (%s)", exc)


def prune(store: dict, now: datetime) -> dict:
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat().replace("+00:00", "Z")
    tools = {}
    for key, row in (store.get("tools") or {}).items():
        seen = [o for o in (row.get("observations") or []) if (o.get("at") or "") >= cutoff]
        if seen:
            tools[key] = {**row, "observations": seen[-OBSERVATIONS_MAX:]}
    return {"format": FORMAT, "tools": tools}


def _same(a: dict, b: dict) -> bool:
    """Two observations that say the same thing: the same money on the same plan with the same free
    tier. Only a change is worth a row; the same price seen again just moves "last checked"."""
    fields = ("amount", "currency", "period", "plan", "freeLimit")
    return all((a.get(f) or None) == (b.get(f) or None) for f in fields)


def observation(price: dict, at: str, source_url: str | None = None, story_slug: str | None = None,
                source: str = "article") -> dict:
    """One row, in the shape the store and the site both read."""
    return {
        "at": at,
        "plan": price.get("plan") or price.get("planName") or "",
        "amount": price.get("amount") if isinstance(price.get("amount"), (int, float)) else None,
        "currency": price.get("currency") or "",
        "period": price.get("period") or "",
        "freeLimit": price.get("freeLimit") if price.get("freeLimit") is not None else (price.get("free_limit") or ""),
        "quoted": price.get("quoted") or "",
        "sourceUrl": source_url or None,
        "storySlug": story_slug or None,
        # "article" (what the coverage stated) or "maker" (the maker's own pricing page).
        "source": "maker" if source == "maker" else "article",
    }


def record(store: dict, key: str, tool: str, maker: str | None, row: dict) -> bool:
    """Add one observation for a tool. True when it changed something.

    A price that says what the newest row already says is not a second row: it only moves the tool's
    "last checked", so "Price checked 22 Sept" stays true without the history filling with copies.
    """
    if not key or not row:
        return False
    entry = store.setdefault("tools", {}).setdefault(key, {"key": key, "tool": tool, "maker": maker, "observations": []})
    entry["tool"] = tool or entry.get("tool") or ""
    entry["maker"] = maker or entry.get("maker")
    seen = entry["observations"]
    if seen and _same(seen[-1], row):
        # Same price: keep the older row's date as when it started, and remember this check.
        seen[-1]["checkedAt"] = row["at"]
        for field in ("quoted", "sourceUrl", "storySlug"):
            if not seen[-1].get(field) and row.get(field):
                seen[-1][field] = row[field]
        return False
    if seen and (row.get("at") or "") < (seen[-1].get("at") or ""):
        return False  # an older observation than the one we have: history only moves forward
    row = {**row, "checkedAt": row["at"]}
    seen.append(row)
    del seen[:-OBSERVATIONS_MAX]
    return True


def record_from_stories(store: dict, stories: list[dict]) -> dict:
    """Every price the run's cards state, newest story last so the history is in order.

    `stories` are the exported shapes (export.py). Only cards with a grounded price are read, so
    nothing here is a figure the article did not have (work.price_grounded).
    """
    stats = {"seen": 0, "changed": 0}
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or ""):
        card = s.get("workCard") or {}
        price = card.get("price")
        if not price:
            continue
        at = s.get("firstPublishedAt") or s.get("updatedAt")
        if not at:
            continue
        stats["seen"] += 1
        row = observation(price, at, source_url=card.get("link"), story_slug=s.get("slug"), source="article")
        if record(store, work.story_tool_key(s), card.get("tool") or "", card.get("maker"), row):
            stats["changed"] += 1
    return stats


# ---------------------------------------------------------------------------- what changed

def _money(row: dict) -> float | None:
    amount = row.get("amount")
    return float(amount) if isinstance(amount, (int, float)) else None


def month_name(iso: str | None) -> str:
    """"August", for "was $12 in August"; the year too when it was not this one."""
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = datetime.now(timezone.utc)
    return d.strftime("%B") if d.year == now.year else d.strftime("%B %Y")


def was_line(observations: list[dict]) -> str:
    """What it was before this: "was $12/mo in August"; "" with only one observation.

    When the money did not move and the free tier did, the line says so instead: repeating today's
    price after the word "was" tells the reader nothing.
    """
    if len(observations or []) < 2:
        return ""
    before, now = observations[-2], observations[-1]
    month = month_name(before.get("at"))
    if not month:
        return ""
    text = work.price_text({"amount": before.get("amount"), "currency": before.get("currency"),
                            "period": before.get("period"), "free_limit": before.get("freeLimit")})
    money_moved = (_money(before), before.get("currency"), before.get("period")) != \
                  (_money(now), now.get("currency"), now.get("period"))
    if not money_moved:
        old_free = (before.get("freeLimit") or "").strip()
        return f"free tier was {old_free} in {month}" if old_free else f"no free tier in {month}"
    return f"was {text} in {month}" if text else ""


def change_kind(before: dict, now: dict) -> str | None:
    """What changed between two observations: "rose", "fell", "opened" (a free tier where there was
    none, or the price going to nothing), "free" (the free tier changed), or None."""
    old, new = _money(before), _money(now)
    old_free, new_free = (before.get("freeLimit") or "").strip(), (now.get("freeLimit") or "").strip()
    if (old is not None and old > 0 and new == 0) or (not old_free and new_free):
        return "opened"
    if old is not None and new is not None and (before.get("currency") or "") == (now.get("currency") or ""):
        if new > old:
            return "rose"
        if new < old:
            return "fell"
    if old_free != new_free:
        return "free"
    return None


def changes(store: dict, limit: int = CHANGES_MAX) -> list[dict]:
    """Tools whose price moved, newest first: what it is now, what it was, and why that is a change.

    Only tools with two or more observations are here: one price is not a change, and /work/prices
    is a page about movement.
    """
    out = []
    for key, row in (store.get("tools") or {}).items():
        seen = row.get("observations") or []
        if len(seen) < 2:
            continue
        before, now = seen[-2], seen[-1]
        kind = change_kind(before, now)
        if not kind:
            continue
        out.append({
            "key": key, "tool": row.get("tool") or "", "maker": row.get("maker"),
            "kind": kind, "at": now.get("at"), "now": now, "before": before,
            "was": was_line(seen),
        })
    out.sort(key=lambda c: c.get("at") or "", reverse=True)
    return out[:limit]


def export_shape(store: dict, now: datetime) -> dict:
    """site/src/data/work-prices.json: the whole history by tool, plus the changes the page leads
    with. Small enough to ship whole (one row per tool per change)."""
    tools = []
    for key, row in (store.get("tools") or {}).items():
        seen = row.get("observations") or []
        if not seen:
            continue
        tools.append({
            "key": key, "tool": row.get("tool") or "", "maker": row.get("maker"),
            "observations": seen,
            # The newest row, and when it was last confirmed: the card's "Price checked 22 Sept".
            "current": seen[-1],
            "checkedAt": seen[-1].get("checkedAt") or seen[-1].get("at"),
            "was": was_line(seen),
        })
    tools.sort(key=lambda t: t.get("checkedAt") or "", reverse=True)
    return {"generatedAt": now.isoformat().replace("+00:00", "Z"), "tools": tools, "changes": changes(store)}


def write_export(store: dict, now: datetime, path=None) -> int:
    """Write the site's copy; return how many tools it carries. Any failure is logged and the rest
    of the export goes on: a missing file leaves /work/prices empty, not broken."""
    path = path or EXPORT_FILE
    data = export_shape(store, now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.warning("prices: export not written (%s)", exc)
    return len(data["tools"])


# ---------------------------------------------------------------------------- the maker's own page

def pricing_urls(link: str | None) -> list[str]:
    """The addresses on the tool's own site that would carry its prices, in the order to try them.
    Empty when the link is not one we would fetch."""
    host = work._host(link)
    if not host or not str(link or "").startswith("https://"):
        return []
    return [f"https://{host}{path}" for path in PRICING_PATHS]


def fetchable(card: dict, story: dict | None = None) -> str | None:
    """Why a card's maker page is not to be read, or None. The same bar as howto.fetchable - never a
    forum, a code host, a social network or a news site, and only the maker's own domain - plus: a
    card that already states a price needs no page."""
    from . import howto

    if card.get("price"):
        return "has a price"
    return howto.fetchable(card, story)


def read_price(card: dict, url: str, fetch=None, ask=None) -> tuple[dict | None, str]:
    """(price, status) for one pricing page: "ok", "none" (the page states no price we can ground)
    or "failed" (the page could not be read, or no model answered).

    Only what the page itself states survives: the figures are checked against the page text by the
    same rule the article's are (work.price_grounded).
    """
    from . import howto

    fetch = fetch or howto.page_text
    ask = ask or howto.ask_model
    text = fetch(url)
    if not text or len(text) < checks.MIN_SOURCE_CHARS:
        return None, "failed"
    text = " ".join(text.split()[:PAGE_WORDS])
    try:
        answer = ask(PROMPT.format(maker=card.get("maker") or "the maker", tool=card.get("tool") or "it", text=text))
    except Exception as exc:  # noqa: BLE001
        log.info("prices: no answer for %s (%s)", url, exc)
        return None, "failed"
    price = work.price_grounded(work._price((answer or {}).get("price")), text)
    return (price, "ok") if price else (None, "none")


def load_page_cache(path=None) -> dict:
    path = path or PAGE_CACHE_FILE
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, EOFError):
        return {}


def save_page_cache(data: dict, now: float | None = None, path=None) -> None:
    path = path or PAGE_CACHE_FILE
    cutoff = (now if now is not None else time.time()) - KEEP_DAYS * 86400
    data = {k: v for k, v in data.items() if float((v or {}).get("at") or 0) >= cutoff}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:  # noqa: BLE001
        log.warning("prices: page cache not saved (%s)", exc)


def _due(entry: dict | None, now: float) -> bool:
    """A page to read: never seen, or old enough to look again. A price is re-read after
    RECHECK_DAYS, which is what makes the history a history."""
    if not entry:
        return True
    age = now - float(entry.get("at") or 0)
    status = entry.get("status")
    return age > {"ok": RECHECK_DAYS, "failed": RETRY_DAYS}.get(status, NONE_DAYS) * 86400


def candidates(stories: list[dict]) -> list[tuple[dict, dict]]:
    """(story, card) for exported cards with no price and a maker page to read, newest first, one
    per tool: reading two pages for one tool would spend the run's budget on the same answer."""
    out, seen = [], set()
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or "", reverse=True):
        card = s.get("workCard")
        if not card or fetchable(card, s):
            continue
        key = work.story_tool_key(s)
        if key in seen:
            continue
        seen.add(key)
        out.append((s, card))
    return out


def run(stories: list[dict] | None = None, fetch=None, ask=None, now: datetime | None = None,
        limit: int = MAX_FETCHES, budget_seconds: float = TIME_BUDGET_SECONDS,
        store: dict | None = None, page_cache: dict | None = None, export_path=None) -> dict:
    """The `prices` step: fill the gaps from the makers' own pricing pages, then rewrite the site's
    copy of the history. Bounded to `limit` pages and `budget_seconds`, and every failure is a
    tool without a price rather than a failed run."""
    stats = {"candidates": 0, "fetched": 0, "found": 0, "none": 0, "failed": 0, "reused": 0,
             "recorded": 0, "tools": 0}
    if stories is None:
        try:
            stories = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {**stats, "skipped": "no export"}
    now = now or datetime.now(timezone.utc)
    own_store, own_cache = store is None, page_cache is None
    store = load() if own_store else store
    page_cache = load_page_cache() if own_cache else page_cache
    stamp, clock, start = now.isoformat().replace("+00:00", "Z"), now.timestamp(), time.time()

    todo = candidates(stories)
    stats["candidates"] = len(todo)
    for story, card in todo:
        key = work.story_tool_key(story)
        urls = pricing_urls(card.get("link"))
        cached = next((page_cache[u] for u in urls if (page_cache.get(u) or {}).get("status") == "ok"
                       and not _due(page_cache.get(u), clock)), None)
        if cached:
            stats["reused"] += 1
            if record(store, key, card.get("tool") or "", card.get("maker"),
                      observation(cached["price"], stamp, source_url=cached.get("url"),
                                  story_slug=story.get("slug"), source="maker")):
                stats["recorded"] += 1
            continue
        if stats["fetched"] >= limit or time.time() - start > budget_seconds:
            break
        found = None
        for url in urls:
            if not _due(page_cache.get(url), clock) or stats["fetched"] >= limit:
                continue
            stats["fetched"] += 1
            price, status = read_price(card, url, fetch=fetch, ask=ask)
            page_cache[url] = {"at": clock, "status": status, "url": url, "price": price}
            stats["found" if status == "ok" else status] += 1
            if price:
                found = (url, price)
                break
        if found and record(store, key, card.get("tool") or "", card.get("maker"),
                            observation(found[1], stamp, source_url=found[0],
                                        story_slug=story.get("slug"), source="maker")):
            stats["recorded"] += 1

    if own_cache:
        save_page_cache(page_cache, clock)
    if own_store:
        save(store, now)
    # The site's copy is only rewritten by a real run; a test that brings its own store says where.
    if export_path is not None or own_store:
        stats["tools"] = write_export(store, now, export_path)
    else:
        stats["tools"] = len(store.get("tools") or {})
    return stats
