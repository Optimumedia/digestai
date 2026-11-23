"""Step: tell Bing, Yandex, Seznam, Naver and the other IndexNow engines about new and updated
pages the moment they exist. Free, no account. The key is a public token proven by a file the
site serves; it is derived from the site name so nothing needs to be configured.

Only pages the site offers to search engines are submitted: a story reported by two or more
publishers, one of the day's most important, or pinned (site/src/lib/indexing.mjs
storySearchWorthy), and threads of five stories or more. Single-publisher stories carry a
"noindex" for Bing; sending Bing a hundred of them a day told it the site is mostly pages it should
not show. The rule is read from the stories.json this run's export step wrote, so it costs no
database reads.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

import requests
from sqlalchemy import select

from . import config, db
from .hold import registrable

log = logging.getLogger("digest.indexnow")

ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_URLS = 500
# The same numbers as site/src/lib/indexing.mjs (SEARCH_SINGLE_SOURCE_IMPORTANCE, THIN_STORY_IMPORTANCE,
# THREAD_SEARCH_MIN); tests/test_site_sitemap.py checks that the two rules agree.
SINGLE_SOURCE_IMPORTANCE = 8
THIN_STORY_IMPORTANCE = 4
THREAD_MIN_STORIES = 5


def _host(url: str) -> str:
    return (url or "").split("//", 1)[-1].split("/", 1)[0]


def search_worthy(story: dict) -> bool:
    """indexing.mjs storySearchWorthy for one exported story."""
    articles = story.get("articles") or []
    importance = story.get("importance") or 0
    if (story.get("articleCount") or 0) <= 1 and not story.get("hasPrimary") and importance <= THIN_STORY_IMPORTANCE:
        return False
    publishers = {registrable(a.get("domain") or _host(a.get("url") or "")) for a in articles} - {""}
    return bool(story.get("pinned")) or len(publishers) >= 2 or importance >= SINGLE_SOURCE_IMPORTANCE


def story_filter(path=None):
    """A test for a story slug: True when it should be submitted. Exported stories pass when search
    engines are offered them; a slug missing from the export (merged into another story, so its
    address now redirects) passes, which tells the engines where it moved. Without an export,
    everything passes."""
    path = path or config.SITE_DATA_DIR / "stories.json"
    try:
        stories = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return lambda slug: True
    exported = {s.get("slug") for s in stories}
    worthy = {s["slug"] for s in stories if s.get("slug") and search_worthy(s)}
    return lambda slug: slug in worthy or slug not in exported


def key() -> str:
    return hashlib.sha256(f"indexnow:{config.SITE_URL}".encode()).hexdigest()[:32]


def run() -> dict:
    stats = {"submitted": 0, "status": None}
    k = key()
    key_file = config.ROOT / "site" / "public" / f"{k}.txt"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(k, encoding="utf-8")

    eng = db.engine()
    since = db.utcnow() - timedelta(minutes=45)
    with eng.connect() as conn:
        rows = conn.execute(
            # A story just merged into another (merge.py) is submitted too: its address now redirects.
            select(db.stories.c.slug).where(db.stories.c.status.in_(["published", "merged"]), db.stories.c.updated_at >= since)
        ).all()
        threads = conn.execute(
            select(db.threads.c.slug).where(db.threads.c.status == "published", db.threads.c.updated_at >= since, db.threads.c.story_count >= THREAD_MIN_STORIES)
        ).all()
    wanted = story_filter()
    keep = [r.slug for r in rows if wanted(r.slug)]
    stats["skipped"] = len(rows) - len(keep)
    urls = [f"{config.SITE_URL}/story/{slug}" for slug in keep] + [f"{config.SITE_URL}/thread/{t.slug}" for t in threads]
    if urls:
        urls += [f"{config.SITE_URL}/", f"{config.SITE_URL}/today", f"{config.SITE_URL}/models", f"{config.SITE_URL}/funding"]
    urls = list(dict.fromkeys(urls))[:MAX_URLS]
    if not urls:
        return stats
    host = config.SITE_URL.split("//", 1)[-1]
    try:
        resp = requests.post(
            ENDPOINT,
            json={"host": host, "key": k, "keyLocation": f"{config.SITE_URL}/{k}.txt", "urlList": urls},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30,
        )
        stats.update(submitted=len(urls), status=resp.status_code)
        if resp.status_code >= 400:
            log.warning("IndexNow answered %s: %s", resp.status_code, resp.text[:120])
    except requests.RequestException as exc:
        stats["status"] = f"error: {exc.__class__.__name__}"
    return stats
