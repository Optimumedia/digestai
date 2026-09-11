"""Step: tell Bing, Yandex, Seznam, Naver and the other IndexNow engines about new and updated
pages the moment they exist. Free, no account. The key is a public token proven by a file the
site serves; it is derived from the site name so nothing needs to be configured.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

import requests
from sqlalchemy import select

from . import config, db

log = logging.getLogger("digest.indexnow")

ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_URLS = 500


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
            select(db.stories.c.slug).where(db.stories.c.status == "published", db.stories.c.updated_at >= since)
        ).all()
        threads = conn.execute(
            select(db.threads.c.slug).where(db.threads.c.status == "published", db.threads.c.updated_at >= since, db.threads.c.story_count >= 2)
        ).all()
    urls = [f"{config.SITE_URL}/story/{r.slug}" for r in rows] + [f"{config.SITE_URL}/thread/{t.slug}" for t in threads]
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
