"""Step: give topic hub pages (companies, models, people) a model-written description so each
hub carries unique text for search engines and readers, not just a list of headlines.

Budgeted: a few descriptions per run, refreshed when a topic's story count grows a lot.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import insert, select, update

from . import config, db, enrich
from .textutil import slugify

log = logging.getLogger("digest.topics")

MAX_PER_RUN = 5
MIN_STORIES = 3
PROMPT = """You write short encyclopedic introductions for topic pages on an AI news site.
Topic: {name} ({kind})
Recent headlines about it:
{headlines}

Return ONLY JSON: {{"description": "<2-3 sentences, 40-70 words: what {name} is, and what the recent news is about. Neutral, factual, no hype, no 'this page', present tense>"}}"""


def run() -> dict:
    stats = {"described": 0, "topics": 0}
    entities_file = config.SITE_DATA_DIR / "entities.json"
    stories_file = config.SITE_DATA_DIR / "stories.json"
    if not entities_file.exists() or not stories_file.exists():
        return stats
    entities = json.loads(entities_file.read_text(encoding="utf-8"))
    stories = {s["id"]: s for s in json.loads(stories_file.read_text(encoding="utf-8"))}
    eng = db.engine()
    now = db.utcnow()

    # Sync the topic table with what the export knows (merged by slug).
    merged: dict[str, dict] = {}
    for e in entities:
        slug = slugify(e["name"])
        if not slug:
            continue
        m = merged.setdefault(slug, {"name": e["name"], "kind": e["kind"], "ids": set()})
        m["ids"].update(e["storyIds"])
    with eng.begin() as conn:
        existing = {t.slug: t for t in conn.execute(select(db.topics)).all()}
        for slug, m in merged.items():
            if slug in existing:
                conn.execute(update(db.topics).where(db.topics.c.slug == slug).values(story_count=len(m["ids"]), updated_at=now))
            else:
                conn.execute(insert(db.topics).values(slug=slug, name=m["name"], kind=m["kind"], story_count=len(m["ids"]), described_at_count=0, updated_at=now))
    stats["topics"] = len(merged)

    if not (config.GROQ_API_KEY or config.GEMINI_API_KEY):
        return stats
    with eng.connect() as conn:
        due = conn.execute(
            select(db.topics).where(db.topics.c.story_count >= MIN_STORIES)
            .order_by(db.topics.c.story_count.desc())
        ).all()
        allowance = enrich.allowance(conn, "groq") if config.GROQ_API_KEY else enrich.allowance(conn, "gemini")
    budget = min(MAX_PER_RUN, allowance)
    for t in due:
        if stats["described"] >= budget:
            break
        # Describe once at 3 stories, refresh when the count has doubled since.
        if t.described_at_count and t.story_count < 2 * t.described_at_count:
            continue
        ids = merged.get(t.slug, {}).get("ids", set())
        heads = [stories[i]["headline"] for i in ids if i in stories][:12]
        if len(heads) < MIN_STORIES:
            continue
        prompt = PROMPT.format(name=t.name, kind={"companies": "company", "models": "AI model", "people": "person"}.get(t.kind, "topic"), headlines="\n".join(f"- {h}" for h in heads))
        try:
            result = enrich.call_groq(prompt) if config.GROQ_API_KEY else enrich.call_gemini(prompt)
            enrich.record_usage(eng, "groq" if config.GROQ_API_KEY else "gemini", 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("topic description failed for %s: %s", t.slug, exc)
            continue
        desc = str(result.get("description") or "").strip()[:600]
        if len(desc) < 40:
            continue
        with eng.begin() as conn:
            conn.execute(update(db.topics).where(db.topics.c.id == t.id).values(description=desc, described_at_count=t.story_count, updated_at=now))
        stats["described"] += 1

    with eng.connect() as conn:
        rows = conn.execute(select(db.topics).where(db.topics.c.description.isnot(None))).all()
    (config.SITE_DATA_DIR / "topics.json").write_text(
        json.dumps({r.slug: {"name": r.name, "kind": r.kind, "description": r.description} for r in rows}, ensure_ascii=False), encoding="utf-8")
    return stats
