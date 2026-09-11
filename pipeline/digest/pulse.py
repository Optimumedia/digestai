"""Step: community pulse. Three sentences on what practitioners are saying, from the top
Hacker News comments on a story, for stories whose thread has real discussion."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import requests
from sqlalchemy import select, update

from . import config, db, enrich

log = logging.getLogger("digest.pulse")

MIN_POINTS = 30
MAX_PER_RUN = 2
MAX_COMMENTS = 25
PROMPT = """Below are top comments from the Hacker News discussion of the article "{title}".
Write 2 to 3 sentences, plain text, summarising what the commenters think: the main reactions,
the strongest objection or caveat, and any expertise they bring. Do not mention "Hacker News",
do not quote usernames, do not start with "Commenters". Return JSON: {{"pulse": "..."}}

Comments:
{comments}
"""


def _strip(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&\w+;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def top_comments(item_id: str) -> list[str]:
    resp = requests.get(f"https://hn.algolia.com/api/v1/items/{item_id}", timeout=config.FETCH_TIMEOUT,
                        headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    out = []
    for c in resp.json().get("children", [])[:MAX_COMMENTS]:
        text = _strip(c.get("text") or "")
        if 40 <= len(text):
            out.append(text[:600])
    return out


def run() -> dict:
    stats = {"summarised": 0, "skipped": 0}
    if not (config.GEMINI_API_KEY or config.GROQ_API_KEY):
        stats["reason"] = "needs an LLM key"
        return stats
    eng = db.engine()
    since = db.utcnow() - timedelta(days=3)
    with eng.connect() as conn:
        rows = conn.execute(
            select(db.stories.c.id, db.stories.c.headline, db.articles.c.discussion_url, db.articles.c.discussion_points)
            .join(db.articles, db.articles.c.story_id == db.stories.c.id)
            .where(db.stories.c.pulse.is_(None), db.stories.c.status == "published", db.stories.c.updated_at >= since,
                   db.articles.c.discussion_site == "hn", db.articles.c.discussion_points >= MIN_POINTS)
            .order_by(db.articles.c.discussion_points.desc())
            .limit(MAX_PER_RUN * 2)
        ).all()
        allowance = enrich.allowance(conn, "gemini") if config.GEMINI_API_KEY else 0
    budget = min(MAX_PER_RUN, allowance) if config.GEMINI_API_KEY else MAX_PER_RUN
    seen = set()
    for row in rows:
        if row.id in seen or stats["summarised"] >= budget:
            continue
        seen.add(row.id)
        m = re.search(r"id=(\d+)", row.discussion_url or "")
        if not m:
            continue
        try:
            comments = top_comments(m.group(1))
        except Exception as exc:  # noqa: BLE001
            log.warning("comments fetch failed for story %s: %s", row.id, exc)
            continue
        if len(comments) < 3:
            stats["skipped"] += 1
            with eng.begin() as conn:
                conn.execute(update(db.stories).where(db.stories.c.id == row.id).values(pulse="", pulse_at=db.utcnow()))
            continue
        prompt = PROMPT.format(title=row.headline, comments="\n\n".join(f"- {c}" for c in comments))
        try:
            if config.GEMINI_API_KEY:
                result = enrich.call_gemini(prompt)
                enrich.record_usage(eng, "gemini", 1)
            else:
                result = enrich.call_groq(prompt)
        except enrich.QuotaExhausted:
            enrich.record_usage(eng, "gemini", 0, exhausted=True)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("pulse failed for story %s: %s", row.id, exc)
            continue
        pulse = str(result.get("pulse") or "").strip()[:900]
        with eng.begin() as conn:
            conn.execute(update(db.stories).where(db.stories.c.id == row.id).values(pulse=pulse, pulse_at=db.utcnow()))
        stats["summarised"] += 1
    return stats
