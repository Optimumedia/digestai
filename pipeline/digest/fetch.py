"""Step 1: pull new links from curated sources into the articles table (status=new)."""
from __future__ import annotations

import calendar
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from dateutil import parser as dateparser
from sqlalchemy import bindparam, insert, select, update

from . import cache, config, db
from .textutil import clean_title, domain_of, is_skipped_domain, normalize_url, simhash, title_year, word_count

log = logging.getLogger("digest.fetch")

SOURCES_FILE = Path(__file__).with_name("sources.yaml")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": config.USER_AGENT, "Accept": "*/*"})

HN_QUERIES = ["AI", "LLM", "OpenAI", "Anthropic", "Claude", "GPT", "Gemini", "language model", "agents"]
# Same threshold as the gate: syndicated copies sit at 0-8, distinct titles at 13+.
TITLE_DUPLICATE_DISTANCE = 10


def recent_titles(conn) -> list[dict]:
    """Title fingerprints of recent articles that are still alive (not rejected)."""
    since = db.utcnow() - timedelta(days=config.MAX_ARTICLE_AGE_DAYS)
    # From the runner's copy (cache.py). Only whether an article has a discussion link matters here
    # (should_merge_discussion), so the link itself is not read.
    rows = sorted((r for r in cache.articles(conn).values()
                   if db.as_utc(r.created_at) >= since and r.status != "rejected" and r.simhash is not None), key=lambda r: r.id)
    return [{"id": r.id, "hash": db.from_signed64(r.simhash), "domain": r.domain,
             "discussion_url": "known" if r.has_discussion_url else None, "points": r.discussion_points} for r in rows]


def near_duplicate(h: int, recent: list[dict], max_distance: int = TITLE_DUPLICATE_DISTANCE) -> dict | None:
    for other in recent:
        if (h ^ other["hash"]).bit_count() <= max_distance:
            return other
    return None


def should_merge_discussion(disc: tuple | None, existing: dict) -> bool:
    """A community item repeating a known article hands over its thread when the article has
    none yet, or when this thread has more points."""
    if not disc or not disc[0] or not disc[1]:
        return False
    if not existing.get("discussion_url"):
        return True
    return (disc[2] or 0) > (existing.get("points") or 0)


_SOURCES_YAML: dict | None = None


def sources_yaml() -> dict:
    """sources.yaml, parsed once per process."""
    global _SOURCES_YAML
    if _SOURCES_YAML is None:
        _SOURCES_YAML = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    return _SOURCES_YAML


def sync_sources(conn) -> None:
    """Upsert the curated sources.yaml into the sources table (keeps runtime columns)."""
    cfg = sources_yaml()
    type_of = {key: t for t, keys in (cfg.get("types") or {}).items() for key in keys}
    existing = {row.key: row for row in conn.execute(select(db.sources)).all()}
    seen = set()
    for item in cfg["sources"]:
        key = item["key"]
        seen.add(key)
        values = curated_values(item, type_of)
        if key in existing:
            # Only rows whose settings changed are written: one statement per source each run was
            # about eight seconds of round trips to change nothing.
            if source_changed(existing[key], values):
                conn.execute(update(db.sources).where(db.sources.c.key == key).values(**values))
        else:
            conn.execute(insert(db.sources).values(key=key, **values))
    # Curated sources removed from the file are disabled, never deleted (articles reference them).
    for key, row in existing.items():
        if key not in seen and not row.discovered and row.enabled:
            conn.execute(update(db.sources).where(db.sources.c.key == key).values(enabled=False))
    # Expire discovered sources.
    conn.execute(
        update(db.sources)
        .where(db.sources.c.discovered.is_(True), db.sources.c.expires_at < db.utcnow())
        .values(enabled=False)
    )
    # And keep at most MAX_DISCOVERED_SOURCES of them (rank prefers its latest picks; here the
    # newest win), so a backlog of search feeds can never dominate a run again.
    from .rank import cap_discovered_sources

    cap_discovered_sources(conn)


def curated_values(item: dict, type_of: dict) -> dict:
    """The sources-table columns sources.yaml sets for one curated source."""
    key = item["key"]
    return {
        "name": item["name"],
        "url": item["url"],
        "kind": item.get("kind", "rss"),
        "source_type": item.get("type") or type_of.get(key) or ("community" if item.get("kind") in ("hn", "reddit", "mastodon") else "press"),
        "category_hint": item.get("category"),
        "weight": float(item.get("weight", 1.0)),
        "fulltext": bool(item.get("fulltext", True)),
        "content_from_feed": bool(item.get("content_from_feed", False)),
        "enabled": bool(item.get("enabled", True)),
        "discovered": False,
    }


def source_changed(row, values: dict) -> bool:
    """True when a stored source differs from what sources.yaml says it should be."""
    for col, want in values.items():
        have = getattr(row, col, None)
        if isinstance(want, float):
            if have is None or abs(float(have) - want) > 1e-9:
                return True
        elif have != want:
            return True
    return False


def _source_cfg(key: str) -> dict:
    """Per-source options that only live in sources.yaml (max_items, agent)."""
    return next((item for item in sources_yaml()["sources"] if item["key"] == key), {})


AI_WORDS = re.compile(r"\b(?:AI|A\.I\.|artificial intelligence|generative|GenAI|ChatGPT|GPT|Gemini|Claude|Copilot|"
                      r"LLMs?|chatbots?|agents?|agentic|machine learning|automation|automate[ds]?|Perplexity|Midjourney|"
                      r"AI Overviews?|AI Mode|Performance Max|Advantage\+)\b", re.I)


def _ai_only(key: str) -> bool:
    """General marketing blogs publish many posts that are not about AI: keep only those whose title or
    summary is, before anything is downloaded or summarised."""
    return bool(_source_cfg(key).get("ai_only"))


def _max_items(key: str) -> int:
    return int(_source_cfg(key).get("max_items", config.MAX_FETCH_PER_SOURCE))


def _headers(key: str) -> dict:
    if _source_cfg(key).get("agent") == "browser":
        return {"User-Agent": config.BROWSER_AGENT}
    return {}


def _parse_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(attr)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    for attr in ("published", "updated", "created", "dc_date"):
        raw = entry.get(attr)
        if raw:
            try:
                dt = dateparser.parse(raw)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


def _feed_image(entry) -> str | None:
    for m in entry.get("media_content", []) or []:
        if m.get("url") and (m.get("medium") == "image" or str(m.get("type", "")).startswith("image")):
            return m["url"]
    for m in entry.get("media_thumbnail", []) or []:
        if m.get("url"):
            return m["url"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return link.get("href")
    return None


def _feed_body(entry) -> str | None:
    content = entry.get("content") or []
    for c in content:
        if c.get("value") and word_count(c["value"]) > 50:
            return c["value"]
    summary = entry.get("summary")
    return summary if summary and word_count(summary) > 50 else None


# Characters XML 1.0 forbids (a stray \x0b or \x1f in one item made a whole feed unreadable), an "&"
# that starts no entity ("R&D"), and anything a server printed before the XML declaration (a PHP
# warning): AI News failed with "not well-formed (invalid token)" on 21 Sep for one of these.
_XML_BAD_CHARS = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_BARE_AMP = re.compile(rb"&(?!(?:[A-Za-z][A-Za-z0-9]{0,31}|#[0-9]{1,7}|#x[0-9A-Fa-f]{1,6});)")
_XML_START = re.compile(rb"<\?xml|<rss\b|<feed\b|<rdf:RDF\b")
_CDATA = re.compile(rb"(<!\[CDATA\[.*?\]\]>)", re.S)


def repair_xml(content: bytes) -> bytes:
    """A feed with the mistakes that make a strict XML parser give up, put right: forbidden control
    characters removed, bare ampersands escaped (outside CDATA, where they are allowed), and
    whatever comes before the XML itself dropped."""
    start = _XML_START.search(content or b"")
    body = _XML_BAD_CHARS.sub(b"", content[start.start():] if start else (content or b""))
    parts = _CDATA.split(body)
    return b"".join(p if p.startswith(b"<![CDATA[") else _XML_BARE_AMP.sub(b"&amp;", p) for p in parts)


def parse_feed(content: bytes):
    """feedparser's result. A feed that is not well-formed is repaired (repair_xml) and read again:
    feedparser's loose fallback either finds nothing or garbles what it finds ("R&D;"), so the
    repaired reading wins whenever it finds at least as many items."""
    feed = feedparser.parse(content)
    if feed.bozo:
        fixed = feedparser.parse(repair_xml(content))
        if fixed.entries and len(fixed.entries) >= len(feed.entries):
            return fixed
    return feed


# Aggregators (Techmeme) link their own page; the article is the first outside link in the item's
# HTML that is not an outlet's home page ("link_from: description" in sources.yaml).
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_OUTLET_SUFFIX = re.compile(r"\s*\((?:[^()]{1,60})\)\s*$")


def link_in_description(html: str, skip_host: str) -> str | None:
    from urllib.parse import urlsplit

    for href in _HREF.findall(html or ""):
        href = href.replace("&amp;", "&").strip()
        parts = urlsplit(href)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            continue
        if host == skip_host or host.endswith("." + skip_host):
            continue
        if parts.path in ("", "/") and not parts.query:
            continue  # the outlet's name links its home page
        return href
    return None


class FeedItems(list):
    """A feed's usable items, plus what the health check needs about the whole feed: how many
    entries it had before any filter, and the date of its newest entry."""
    entries: int = 0
    newest: datetime | None = None


def _entry_items(source, feed, from_fallback: bool = False) -> FeedItems:
    cfg = _source_cfg(source.key)
    ai_only = _ai_only(source.key)
    link_from = cfg.get("link_from")
    # keep_domain filters the fallback (a news search that also finds other sites); the source's own
    # feed is never filtered by it. Applied to both, the EU AI Act newsletter's Substack items
    # (artificialintelligenceact.substack.com) were all dropped for not being on its old .eu site.
    keep_domain = ((cfg.get("keep_domain") or "").lower() or None) if from_fallback else None
    feed_host = domain_of(source.url)
    items = FeedItems()
    items.entries = len(feed.entries)
    dates = [d for d in (_parse_date(e) for e in feed.entries) if d is not None]
    items.newest = max(dates) if dates else None
    for e in feed.entries:
        link = e.get("link") or ""
        title = e.get("title") or ""
        if link_from == "description":
            # The aggregator is only how the article was found: its original address is stored, so
            # the outlet is credited and the outlet's own feed finds it already known.
            link = link_in_description(e.get("summary") or "", feed_host) or ""
            title = _OUTLET_SUFFIX.sub("", title)  # "... (Bloomberg)"
        if not link.startswith("http"):
            continue
        if keep_domain:
            try:
                dom = domain_of(normalize_url(link))
            except ValueError:
                continue
            if not dom or not (dom == keep_domain or dom.endswith("." + keep_domain)):
                continue
        if ai_only and not AI_WORDS.search(f"{title} {re.sub(r'<[^>]+>', ' ', e.get('summary') or '')[:600]}"):
            continue
        description = e.get("summary") or ""
        if link_from == "description":
            description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", description)).strip()
        items.append({
            "url": link,
            "title": title,
            "published_at": _parse_date(e),
            "description": description[:2000],
            "feed_content": None if link_from == "description" else _feed_body(e),
            "image_url": None if link_from == "description" else _feed_image(e),
            "author": None if link_from == "description" else ((e.get("author") or "")[:300] or None),
        })
    return items


def _rss_items(source) -> FeedItems:
    """The feed's items. When the feed fails (an error status, or nothing readable) and sources.yaml
    names a `fallback` feed, that one is read instead (Unite.AI answers 503 to some runs)."""
    fallback = _source_cfg(source.key).get("fallback")
    try:
        resp = SESSION.get(source.url, timeout=config.FETCH_TIMEOUT, headers=_headers(source.key))
        resp.raise_for_status()
        feed = parse_feed(resp.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"unparseable feed: {feed.bozo_exception}")
        if fallback and not feed.entries:
            raise ValueError("empty feed")  # an HTML page served at the feed's address reads as empty
    except Exception:
        if not fallback:
            raise
        resp = SESSION.get(fallback, timeout=config.FETCH_TIMEOUT)
        resp.raise_for_status()
        feed = parse_feed(resp.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"unparseable feed and fallback: {feed.bozo_exception}") from None
        return _entry_items(source, feed, from_fallback=True)
    return _entry_items(source, feed)


def _hn_items(source) -> list[dict]:
    since = int((db.utcnow() - timedelta(days=2)).timestamp())
    items, seen = [], set()
    for q in HN_QUERIES:
        resp = SESSION.get(
            source.url,
            params={"tags": "story", "query": q, "numericFilters": f"points>40,created_at_i>{since}", "hitsPerPage": 30},
            timeout=config.FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        for hit in resp.json().get("hits", []):
            url = hit.get("url")
            if not url or hit["objectID"] in seen:
                continue
            seen.add(hit["objectID"])
            items.append({
                "url": url,
                "title": hit.get("title") or "",
                "published_at": datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
                "description": None,
                "feed_content": None,
                "image_url": None,
                "author": None,
                "discussion": ("hn", f"https://news.ycombinator.com/item?id={hit['objectID']}", int(hit.get("points") or 0)),
            })
        time.sleep(0.3)
    return items


_REDDIT_LINK = re.compile(r'<a href="([^"]+)">\s*\[link\]\s*</a>', re.IGNORECASE)


def _reddit_items(source) -> list[dict]:
    """Reddit blocks its JSON API for unknown clients but serves subreddit RSS.
    Each entry links to the comments page; the external URL sits in the body as [link]."""
    resp = SESSION.get(source.url, timeout=config.FETCH_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        body = e.get("summary") or ""
        m = _REDDIT_LINK.search(body)
        if not m:
            continue
        url = m.group(1).replace("&amp;", "&")
        if "reddit.com" in url or "redd.it" in url or not url.startswith("http"):
            continue
        items.append({
            "url": url,
            "title": e.get("title") or "",
            "published_at": _parse_date(e),
            "description": None,
            "feed_content": None,
            "image_url": None,
            "author": None,
            "discussion": ("reddit", e.get("link"), None),
        })
    return items


def _mastodon_items(source) -> list[dict]:
    """Mastodon's public trending-links API: what the fediverse is sharing right now.
    No account needed. `history` carries daily share counts; the AI gate filters the rest."""
    resp = SESSION.get(source.url, timeout=config.FETCH_TIMEOUT, params={"limit": 40})
    resp.raise_for_status()
    items = []
    for card in resp.json():
        url = card.get("url")
        if not url or not card.get("title"):
            continue
        hist = card.get("history") or []
        shares = sum(int(h.get("uses", 0)) for h in hist[:2])
        published = card.get("published_at")
        items.append({
            "url": url,
            "title": card.get("title") or "",
            "published_at": dateparser.parse(published) if published else None,
            "description": (card.get("description") or "")[:2000] or None,
            "feed_content": None,
            "image_url": card.get("image"),
            "author": (card.get("author_name") or "")[:300] or None,
            "trend_score": shares,
        })
    return items


FETCHERS = {"rss": _rss_items, "hn": _hn_items, "reddit": _reddit_items, "mastodon": _mastodon_items}


def host_delay(host: str) -> float:
    """Politeness between requests to one host: reddit.com throttles back-to-back requests;
    others (e.g. the Bing search feeds sharing bing.com) only need a short gap."""
    return config.FETCH_SLOW_HOSTS.get(host, config.FETCH_HOST_DELAY)


# ------------------------------------------------------------------ source health
# A source that keeps failing is asked less often, and one that answers "429 Too Many Requests" is
# left alone for a while at once: Reddit answered 404 to 535 runs in a row (17 to 26 Nov 2026) and
# MarTech sometimes answers 429. The wait doubles with each failure in a row, up to a day, so a
# source that recovers is found again within a day. Only columns fetch already reads are used.
FAILURES_BEFORE_BACKOFF = 3
MAX_BACKOFF_HOURS = 24.0
# A feed that answers but whose newest entry is this old has most likely stopped updating: the
# Anthropic and Meta AI mirrors went quiet on 17 Oct and 26 Jul 2026 while every run read them
# "successfully". Per-source `stale_days` in sources.yaml overrides it for feeds that post often.
STALE_DAYS = 60


class Skipped(Exception):
    """A source not asked this run (its host answered 429 earlier in the run)."""


def rate_limited(error) -> bool:
    """True for a 429 answer, given the exception or the stored last_error text."""
    if isinstance(error, BaseException):
        resp = getattr(error, "response", None)
        if getattr(resp, "status_code", None) == 429:
            return True
        error = str(error)
    return bool(error) and bool(re.match(r"\s*429\b", str(error)))


def backoff_hours(error_count: int, last_error: str | None) -> float:
    """How long a source rests after `error_count` failures in a row. A 429 rests from the first
    one (the publisher asked us to slow down); other errors get FAILURES_BEFORE_BACKOFF tries first,
    since a single timeout says little."""
    if not error_count or error_count <= 0:
        return 0.0
    if rate_limited(last_error):
        exponent = error_count - 1
    elif error_count < FAILURES_BEFORE_BACKOFF:
        return 0.0
    else:
        exponent = error_count - FAILURES_BEFORE_BACKOFF
    return min(2.0 ** min(exponent, 10), MAX_BACKOFF_HOURS)


def resting(source, now: datetime | None = None) -> bool:
    """True when the source failed recently enough that it is not asked this run."""
    hours = backoff_hours(getattr(source, "error_count", 0) or 0, getattr(source, "last_error", None))
    last = db.as_utc(getattr(source, "last_fetched_at", None))
    if not hours or last is None:
        return False
    return (now or db.utcnow()) - last < timedelta(hours=hours)


def health_note(key: str, items, now: datetime | None = None) -> str | None:
    """What is wrong with a feed that answered: no entries at all, or a newest entry so old the feed
    has probably stopped. Stored as the source's last_error (the admin page shows it) without
    counting as a failure, so it never slows the source down."""
    entries = getattr(items, "entries", None)
    if entries is None:
        entries = len(items or [])
    if not entries:
        return "empty: the feed answered with no entries"
    newest = getattr(items, "newest", None)
    if newest is None:
        dates = [i.get("published_at") for i in items or [] if i.get("published_at")]
        newest = max(dates) if dates else None
    if newest is None:
        return None
    days = int(_source_cfg(key).get("stale_days") or STALE_DAYS)
    age = (now or db.utcnow()) - db.as_utc(newest)
    if age > timedelta(days=days):
        return f"stale: newest entry {newest:%Y-%m-%d} ({age.days} days old)"
    return None


def newest_first(items: list[dict]) -> list[dict]:
    """Items sorted newest first (undated ones last, in feed order), so max_items keeps the newest
    even from a feed that lists oldest first."""
    dated = [i for i in items if i.get("published_at")]
    undated = [i for i in items if not i.get("published_at")]
    dated.sort(key=lambda i: db.as_utc(i["published_at"]), reverse=True)
    return dated + undated


def fetch_all(source_rows, fetchers: dict | None = None, sleep=time.sleep) -> list[tuple]:
    """Download every source: hosts in parallel, one host's sources in sequence with its delay.
    Returns (source, items, error) in the original source order, so inserts stay deterministic.
    After a 429 the host's remaining sources are not asked this run (error is a Skipped)."""
    fetchers = fetchers or FETCHERS
    by_host: dict[str, list] = {}
    for s in source_rows:
        by_host.setdefault(domain_of(s.url), []).append(s)

    def work(group: list) -> list[tuple]:
        out = []
        paused = False
        for i, source in enumerate(group):
            if paused:
                out.append((source, None, Skipped(f"{domain_of(source.url)} answered 429 earlier in this run")))
                continue
            if i:
                sleep(host_delay(domain_of(source.url)))
            try:
                out.append((source, fetchers.get(source.kind, fetchers["rss"])(source), None))
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                out.append((source, None, exc))
                paused = rate_limited(exc)
        return out

    results: list[tuple] = []
    if by_host:
        with ThreadPoolExecutor(max_workers=max(1, min(config.FETCH_WORKERS, len(by_host)))) as pool:
            for chunk in pool.map(work, by_host.values()):
                results.extend(chunk)
    order = {id(s): i for i, s in enumerate(source_rows)}
    results.sort(key=lambda r: order[id(r[0])])
    return results


def run() -> dict:
    stats = {"sources": 0, "items": 0, "inserted": 0, "errors": 0}
    eng = db.engine()
    with eng.begin() as conn:
        sync_sources(conn)
        source_rows = conn.execute(select(db.sources).where(db.sources.c.enabled.is_(True))).all()
        recent = recent_titles(conn)
        known = cache.known_urls(conn)

    now = db.utcnow()
    due = [s for s in source_rows if not resting(s, now)]
    rested = [s.key for s in source_rows if s not in due]
    if rested:
        stats["resting"] = len(rested)
        log.info("resting after repeated failures or a 429: %s", ", ".join(sorted(rested)))

    cutoff = now - timedelta(days=config.MAX_ARTICLE_AGE_DAYS)
    this_year = now.year
    # Each source's outcome, written in one statement at the end: a transaction per source was about
    # half a second of round trips each, some 45 seconds a run, for sources with nothing new.
    outcomes: list[dict] = []
    try:
        for source, items, exc in fetch_all(due):
            stats["sources"] += 1
            if isinstance(exc, Skipped):
                stats["skipped"] = stats.get("skipped", 0) + 1
                log.info("source %s not asked: %s", source.key, exc)
                continue
            if exc is not None:  # one bad source must not stop the run
                stats["errors"] += 1
                if rate_limited(exc):
                    stats["rate_limited"] = stats.get("rate_limited", 0) + 1
                log.warning("source %s failed: %s", source.key, exc)
                outcomes.append({"sid": source.id, "err": str(exc)[:500], "cnt": (source.error_count or 0) + 1})
                continue
            note = health_note(source.key, items, now)
            if note:
                stats["unhealthy"] = stats.get("unhealthy", 0) + 1
                log.warning("source %s answered but looks unhealthy: %s", source.key, note)
            outcomes.append({"sid": source.id, "err": note, "cnt": 0})

            limit = _max_items(source.key) if not source.discovered else config.MAX_DISCOVERED_ITEMS
            candidates = []
            for item in newest_first(items)[:limit]:
                stats["items"] += 1
                try:
                    url = normalize_url(item["url"])
                except ValueError:
                    continue
                dom = domain_of(url)
                if not dom or is_skipped_domain(dom):
                    continue
                if item["published_at"] and item["published_at"] < cutoff:
                    continue
                # "... (2019)": an old piece re-posted to HN/Reddit. The submission time is new,
                # the story is not (MAX_ARTICLE_AGE_DAYS applies to the piece, not the re-post).
                year = title_year(item["title"])
                if year and year < this_year:
                    stats["too_old_title_year"] = stats.get("too_old_title_year", 0) + 1
                    continue
                # Addresses this runner saw in the database before are not asked about again: feeds
                # repeat their items for days, and each question is a round trip the plan meters.
                url_key = cache.url_hash(url)
                if url_key in known:
                    continue
                title = clean_title(item["title"])
                if len(title) < 15:
                    continue
                candidates.append((item, url, dom, url_key, title))
            inserted = 0
            added: list[str] = []
            if not candidates:  # nothing the database has to be asked about
                log.info("%-22s %3d items, %2d new", source.key, len(items), 0)
                continue
            with eng.begin() as conn:
                for item, url, dom, url_key, title in candidates:
                    exists = conn.execute(select(db.articles.c.id).where(db.articles.c.url == url)).first()
                    if exists:
                        known[url_key] = time.time()
                        continue
                    disc = item.get("discussion") or (None, None, None)
                    h = simhash(title)
                    dup = near_duplicate(h, recent)
                    if dup is not None:
                        if should_merge_discussion(item.get("discussion"), dup):
                            conn.execute(update(db.articles).where(db.articles.c.id == dup["id"]).values(
                                discussion_site=disc[0], discussion_url=disc[1], discussion_points=disc[2],
                                discussion_checked_at=db.utcnow()))
                            dup.update(discussion_url=disc[1], points=disc[2])
                            stats["discussions_merged"] = stats.get("discussions_merged", 0) + 1
                        # A repeat from the same publisher, or any search-feed repeat, is never fetched.
                        # Other publishers' copies still go through (the gate keeps the first one).
                        if dup["domain"] == dom or source.discovered:
                            stats["title_duplicates"] = stats.get("title_duplicates", 0) + 1
                            continue
                    res = conn.execute(insert(db.articles).values(
                        discussion_site=disc[0],
                        discussion_url=disc[1],
                        discussion_points=disc[2],
                        trend_score=item.get("trend_score"),
                        discussion_checked_at=db.utcnow() if disc[0] else None,
                        url=url,
                        source_id=source.id,
                        raw_title=item["title"][:500],
                        title=title[:500],
                        author=item["author"],
                        domain=dom[:200],
                        published_at=item["published_at"],
                        fetched_at=db.utcnow(),
                        description=item["description"] or None,
                        feed_content=item["feed_content"],
                        image_url=item["image_url"],
                        show_fulltext=bool(source.fulltext),
                        simhash=db.to_signed64(h),
                        status="new",
                        created_at=db.utcnow(),
                    ))
                    recent.append({"id": res.inserted_primary_key[0], "hash": h, "domain": dom,
                                   "discussion_url": disc[1], "points": disc[2]})
                    inserted += 1
                    added.append(url_key)
            # Remembered only once the insert is committed.
            for url_key in added:
                known[url_key] = time.time()
            stats["inserted"] += inserted
            log.info("%-22s %3d items, %2d new", source.key, len(items), inserted)
    finally:
        # Also when an insert fails: the sources fetched so far keep their status.
        record_outcomes(eng, outcomes)
    return stats


def record_outcomes(eng, outcomes: list[dict], when: datetime | None = None) -> None:
    """Every fetched source's last_fetched_at, last_error and error_count in one executemany."""
    if not outcomes:
        return
    with eng.begin() as conn:
        conn.execute(
            update(db.sources).where(db.sources.c.id == bindparam("sid"))
            .values(last_fetched_at=when or db.utcnow(), last_error=bindparam("err"), error_count=bindparam("cnt")),
            outcomes,
        )
