"""Step: export published stories, the daily briefing and topic data as JSON for the site build."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import select

from . import config, db
from .textutil import word_count

log = logging.getLogger("digest.export")

BRIEFING_SIZE = 5
BRIEFING_ALSO = 8


def _iso(dt):
    dt = db.as_utc(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _coverage(articles: list[dict]) -> dict:
    cov = {"primary": 0, "press": 0, "newsletter": 0, "community": 0}
    for a in articles:
        cov[a["sourceType"]] = cov.get(a["sourceType"], 0) + 1
    return cov


def build_briefing(stories: list[dict], now) -> dict:
    """Top stories of the last 24 hours (48 on a quiet day), ranked by score."""
    for window in (24, 48, 96):
        cutoff = (now - timedelta(hours=window)).isoformat()
        pool = [s for s in stories if (s["updatedAt"] or "") >= cutoff]
        if len(pool) >= BRIEFING_SIZE or window == 96:
            break
    pool.sort(key=lambda s: (not s["pinned"], -s["score"]))
    top = pool[:BRIEFING_SIZE]
    also = pool[BRIEFING_SIZE : BRIEFING_SIZE + BRIEFING_ALSO]
    words = sum(word_count(s.get("summaryMd") or "") for s in top) + 25 * len(also)
    return {
        "date": now.date().isoformat(),
        "generatedAt": _iso(now),
        "windowHours": window,
        "storyIds": [s["id"] for s in top],
        "alsoIds": [s["id"] for s in also],
        "stats": {
            "stories": len(pool),
            "articles": sum(s["articleCount"] for s in pool),
            "minutes": max(3, round(words / 230)),
        },
    }


def run() -> dict:
    eng = db.engine()
    out_dir = config.SITE_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    now = db.utcnow()
    since = now - timedelta(days=config.EXPORT_DAYS)

    with eng.connect() as conn:
        src_rows = conn.execute(select(db.sources)).all()
        sources = {
            s.id: {"key": s.key, "name": s.name, "url": s.url, "kind": s.kind, "type": s.source_type,
                   "weight": s.weight, "discovered": s.discovered, "enabled": s.enabled}
            for s in src_rows
        }
        story_rows = conn.execute(
            select(db.stories).where(db.stories.c.status == "published", db.stories.c.updated_at >= since)
            .order_by(db.stories.c.updated_at.desc())
        ).all()
        story_ids = [s.id for s in story_rows]
        art_rows = []
        for i in range(0, len(story_ids), 500):
            chunk = story_ids[i : i + 500]
            art_rows.extend(conn.execute(
                select(db.articles).where(db.articles.c.story_id.in_(chunk), db.articles.c.status == "published")
                .order_by(db.articles.c.published_at.desc())
            ).all())
        sent = {n.date: {"publicUrl": n.public_url, "subject": n.subject} for n in conn.execute(select(db.newsletters)).all()}

    by_story: dict[int, list] = {}
    for a in art_rows:
        by_story.setdefault(a.story_id, []).append(a)

    stories_out: list[dict] = []
    entity_index: dict[str, dict] = {}
    for s in story_rows:
        members = by_story.get(s.id, [])
        if not members:
            continue
        lead = next((m for m in members if m.id == s.lead_article_id), members[0])
        articles = []
        for m in members:
            src = sources.get(m.source_id, {})
            stype = src.get("type") or "press"
            community = stype == "community" or src.get("discovered")
            # Community feeds point at other publishers: credit the publisher, keep the community as "via".
            articles.append({
                "id": m.id,
                "slug": m.slug,
                "url": m.url,
                "domain": m.domain,
                "source": m.domain if community else src.get("name"),
                "via": src.get("name") if community else None,
                "sourceKey": src.get("key"),
                "sourceType": "press" if community else stype,
                "title": m.title,
                "headline": m.headline,
                "author": m.author,
                "publishedAt": _iso(m.published_at) or _iso(m.fetched_at),
                "description": m.description,
                "contentMd": m.content_md if m.show_fulltext and m.content_md else None,
                "wordCount": m.word_count,
                "imageUrl": m.image_url,
                "summaryMd": m.summary_md,
                "keyPoints": m.key_points or [],
                "whyItMatters": m.why_it_matters,
                "category": m.category,
                "entities": m.entities or {},
                "contentType": m.content_type,
                "importance": m.importance,
                "predictedScore": m.predicted_score,
                "isLead": m.id == lead.id,
                "discussion": (
                    {"site": m.discussion_site, "url": m.discussion_url, "points": m.discussion_points}
                    if m.discussion_url else None
                ),
            })
        # A primary source is one the story is *about*: the lab's own post counts even when it arrived via HN.
        for a in articles:
            if a["domain"] in PRIMARY_DOMAINS:
                a["sourceType"] = "primary"
        coverage = _coverage(articles)
        discussions = sorted(
            (a["discussion"] for a in articles if a["discussion"]),
            key=lambda d: -(d["points"] or 0),
        )
        story = {
            "id": s.id,
            "slug": s.slug,
            "headline": s.headline,
            "summaryMd": s.summary_md,
            "keyPoints": s.key_points or [],
            "whyItMatters": s.why_it_matters,
            "category": s.category,
            "categoryName": config.CATEGORIES.get(s.category or "", "AI"),
            "entities": s.entities or {},
            "importance": s.importance,
            "score": s.score,
            "pinned": s.pinned,
            "articleCount": len(articles),
            "coverage": coverage,
            "hasPrimary": coverage["primary"] > 0,
            "discussions": discussions,
            "firstPublishedAt": _iso(s.first_published_at),
            "updatedAt": _iso(s.updated_at),
            "imageUrl": lead.image_url or next((a["imageUrl"] for a in articles if a["imageUrl"]), None),
            "ogImage": f"/og/{s.slug}.png",
            "leadArticleId": lead.id,
            "articles": articles,
        }
        stories_out.append(story)
        for kind in ("companies", "models", "people"):
            for name in (s.entities or {}).get(kind, []) or []:
                key = name.strip()
                if not key:
                    continue
                ent = entity_index.setdefault(key, {"name": key, "kind": kind, "storyIds": []})
                ent["storyIds"].append(s.id)

    entities_out = sorted(
        (e for e in entity_index.values() if len(e["storyIds"]) >= 2),
        key=lambda e: len(e["storyIds"]), reverse=True,
    )
    briefing = build_briefing(stories_out, now)

    (out_dir / "stories.json").write_text(json.dumps(stories_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "entities.json").write_text(json.dumps(entities_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "briefing.json").write_text(json.dumps(briefing, ensure_ascii=False), encoding="utf-8")
    (out_dir / "newsletters.json").write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    (out_dir / "sources.json").write_text(
        json.dumps([v for v in sources.values() if v["enabled"] and not v["discovered"]], ensure_ascii=False), encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps({
        "generatedAt": _iso(now),
        "siteUrl": config.SITE_URL,
        "categories": config.CATEGORIES,
        "storyCount": len(stories_out),
        "articleCount": sum(s["articleCount"] for s in stories_out),
    }), encoding="utf-8")
    return {"stories": len(stories_out), "entities": len(entities_out), "briefing": len(briefing["storyIds"]), "dir": str(out_dir)}


# Domains whose posts are the primary source of a story regardless of which feed found them.
PRIMARY_DOMAINS = {
    "openai.com", "anthropic.com", "claude.com", "deepmind.google", "blog.google", "research.google",
    "ai.meta.com", "about.fb.com", "blogs.nvidia.com", "nvidia.com", "huggingface.co", "mistral.ai",
    "microsoft.com", "blogs.microsoft.com", "azure.microsoft.com", "aws.amazon.com", "machinelearning.apple.com",
    "x.ai", "cohere.com", "stability.ai", "arxiv.org", "github.com", "deepseek.com", "qwenlm.github.io",
    "ai.google.dev", "cloud.google.com", "apple.com", "meta.com", "perplexity.ai", "cursor.com",
    "europa.eu", "whitehouse.gov", "gov.uk", "nist.gov", "ftc.gov", "sec.gov",
}
