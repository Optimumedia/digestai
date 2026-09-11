"""Step 1: pull new links from curated sources into the articles table (status=new)."""
from __future__ import annotations

import calendar
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from dateutil import parser as dateparser
from sqlalchemy import insert, select, update

from . import config, db
from .textutil import clean_title, domain_of, normalize_url, simhash, word_count

log = logging.getLogger("digest.fetch")

SOURCES_FILE = Path(__file__).with_name("sources.yaml")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": config.USER_AGENT, "Accept": "*/*"})

HN_QUERIES = ["AI", "LLM", "OpenAI", "Anthropic", "Claude", "GPT", "Gemini", "language model", "agents"]
SKIP_DOMAINS = {"news.google.com", "google.com", "youtube.com", "youtu.be", "x.com", "twitter.com",
                "facebook.com", "instagram.com", "tiktok.com", "linkedin.com"}


def sync_sources(conn) -> None:
    """Upsert the curated sources.yaml into the sources table (keeps runtime columns)."""
    cfg = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    type_of = {key: t for t, keys in (cfg.get("types") or {}).items() for key in keys}
    existing = {row.key: row for row in conn.execute(select(db.sources)).all()}
    seen = set()
    for item in cfg["sources"]:
        key = item["key"]
        seen.add(key)
        values = {
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
        if key in existing:
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


_SOURCE_CFG: dict[str, dict] | None = None


def _source_cfg(key: str) -> dict:
    """Per-source options that only live in sources.yaml (max_items, agent)."""
    global _SOURCE_CFG
    if _SOURCE_CFG is None:
        cfg = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
        _SOURCE_CFG = {item["key"]: item for item in cfg["sources"]}
    return _SOURCE_CFG.get(key, {})


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


def _rss_items(source) -> list[dict]:
    resp = SESSION.get(source.url, timeout=config.FETCH_TIMEOUT, headers=_headers(source.key))
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.bozo_exception}")
    items = []
    for e in feed.entries:
        link = e.get("link") or ""
        if not link.startswith("http"):
            continue
        items.append({
            "url": link,
            "title": e.get("title") or "",
            "published_at": _parse_date(e),
            "description": (e.get("summary") or "")[:2000],
            "feed_content": _feed_body(e),
            "image_url": _feed_image(e),
            "author": (e.get("author") or "")[:300] or None,
        })
    return items


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


def run() -> dict:
    stats = {"sources": 0, "items": 0, "inserted": 0, "errors": 0}
    eng = db.engine()
    with eng.begin() as conn:
        sync_sources(conn)
        source_rows = conn.execute(select(db.sources).where(db.sources.c.enabled.is_(True))).all()

    cutoff = db.utcnow() - timedelta(days=config.MAX_ARTICLE_AGE_DAYS)
    last_hit_by_host: dict[str, float] = {}
    for source in source_rows:
        stats["sources"] += 1
        fetcher = FETCHERS.get(source.kind, _rss_items)
        # Politeness: hosts such as reddit.com throttle back-to-back requests.
        host = domain_of(source.url)
        wait = 4.0 - (time.time() - last_hit_by_host.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        last_hit_by_host[host] = time.time()
        try:
            items = fetcher(source)
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            stats["errors"] += 1
            log.warning("source %s failed: %s", source.key, exc)
            with eng.begin() as conn:
                conn.execute(
                    update(db.sources).where(db.sources.c.id == source.id)
                    .values(last_error=str(exc)[:500], error_count=source.error_count + 1, last_fetched_at=db.utcnow())
                )
            continue

        limit = _max_items(source.key) if not source.discovered else 15
        inserted = 0
        with eng.begin() as conn:
            for item in items[:limit]:
                stats["items"] += 1
                try:
                    url = normalize_url(item["url"])
                except ValueError:
                    continue
                dom = domain_of(url)
                if dom in SKIP_DOMAINS or not dom:
                    continue
                if item["published_at"] and item["published_at"] < cutoff:
                    continue
                exists = conn.execute(select(db.articles.c.id).where(db.articles.c.url == url)).first()
                if exists:
                    continue
                title = clean_title(item["title"])
                if len(title) < 15:
                    continue
                disc = item.get("discussion") or (None, None, None)
                conn.execute(insert(db.articles).values(
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
                    simhash=db.to_signed64(simhash(title)),
                    status="new",
                    created_at=db.utcnow(),
                ))
                inserted += 1
            conn.execute(
                update(db.sources).where(db.sources.c.id == source.id)
                .values(last_fetched_at=db.utcnow(), last_error=None, error_count=0)
            )
        stats["inserted"] += inserted
        log.info("%-22s %3d items, %2d new", source.key, len(items), inserted)
    return stats
