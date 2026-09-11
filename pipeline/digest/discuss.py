"""Step: find the Hacker News thread for articles that arrived via feeds.

Community links already carry their thread; everything else is looked up once on the free
Algolia HN API by URL, and re-checked a day later because threads often appear after the
article does.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import requests
from sqlalchemy import or_, select, update

from . import config, db
from .textutil import normalize_url

log = logging.getLogger("digest.discuss")

API = "https://hn.algolia.com/api/v1/search"
MAX_PER_RUN = 60
MIN_POINTS = 5


def lookup(url: str) -> tuple[str, int] | None:
    resp = requests.get(
        API,
        params={"query": url, "restrictSearchableAttributes": "url", "tags": "story", "hitsPerPage": 5},
        timeout=config.FETCH_TIMEOUT,
        headers={"User-Agent": config.USER_AGENT},
    )
    resp.raise_for_status()
    best = None
    for hit in resp.json().get("hits", []):
        hit_url = hit.get("url") or ""
        try:
            same = normalize_url(hit_url) == url
        except ValueError:
            same = False
        if not same:
            continue
        points = int(hit.get("points") or 0)
        if points >= MIN_POINTS and (best is None or points > best[1]):
            best = (f"https://news.ycombinator.com/item?id={hit['objectID']}", points)
    return best


def run() -> dict:
    stats = {"checked": 0, "found": 0, "errors": 0}
    eng = db.engine()
    now = db.utcnow()
    recheck_before = now - timedelta(hours=20)
    fresh_after = now - timedelta(days=3)
    with eng.connect() as conn:
        rows = conn.execute(
            select(db.articles.c.id, db.articles.c.url)
            .where(
                db.articles.c.status == "published",
                db.articles.c.created_at >= fresh_after,
                db.articles.c.discussion_site.is_(None),
                or_(db.articles.c.discussion_checked_at.is_(None), db.articles.c.discussion_checked_at < recheck_before),
            )
            .order_by(db.articles.c.created_at.desc())
            .limit(MAX_PER_RUN)
        ).all()
    for row in rows:
        stats["checked"] += 1
        values = {"discussion_checked_at": db.utcnow()}
        try:
            found = lookup(row.url)
            if found:
                values.update(discussion_site="hn", discussion_url=found[0], discussion_points=found[1])
                stats["found"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            log.warning("hn lookup failed for #%s: %s", row.id, exc)
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id == row.id).values(**values))
        time.sleep(0.25)
    return stats
