"""Step: export published stories, the daily briefing and topic data as JSON for the site build."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import select, update

from . import config, db, hold, trackers as tracker_rules
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
    """Today's top stories: first new ones, then developing ones only to fill empty places.

    New means first published in the last 24 hours (48 or 96 on a quiet day). A developing
    story is older but had at least two articles published inside the window; it only enters
    when there are not enough new stories, so the briefing always leads with fresh news."""

    def new_story(s: dict, cutoff: str) -> bool:
        return bool(s["pinned"] or (s["firstPublishedAt"] or "") >= cutoff)

    def developing(s: dict, cutoff: str) -> bool:
        return sum(1 for a in s["articles"] if (a["publishedAt"] or "") >= cutoff) >= 2

    rank = lambda s: (not s["pinned"], -s["score"])  # noqa: E731
    for window in (24, 48, 96):
        cutoff = _iso(now - timedelta(hours=window))
        fresh = sorted((s for s in stories if new_story(s, cutoff)), key=rank)
        if len(fresh) >= BRIEFING_SIZE + BRIEFING_ALSO or window == 96:
            break
    older = sorted((s for s in stories if not new_story(s, cutoff) and developing(s, cutoff)), key=rank)
    pool = fresh + older
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


def load_moderation() -> dict:
    from pathlib import Path

    import yaml

    path = Path(__file__).with_name("moderation.yaml")
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def apply_moderation(eng, rules: dict | None = None) -> dict:
    """moderation.yaml is the unpublish button when there is no database console."""
    from sqlalchemy import update

    rules = load_moderation() if rules is None else rules
    if not rules:
        return {}
    unpublish = [s.strip() for s in rules.get("unpublish") or [] if s]
    pin = [s.strip() for s in rules.get("pin") or [] if s]
    domains = [d.strip().lower() for d in rules.get("block_domains") or [] if d]
    words = [w.strip().lower() for w in rules.get("block_title_words") or [] if w]
    stats = {"unpublished": 0, "pinned": 0}
    with eng.begin() as conn:
        if unpublish:
            res = conn.execute(update(db.stories).where(db.stories.c.slug.in_(unpublish), db.stories.c.status.in_(["published", hold.HELD]))
                               .values(status="unpublished"))
            stats["unpublished"] = res.rowcount
        conn.execute(update(db.stories).where(db.stories.c.pinned.is_(True), ~db.stories.c.slug.in_(pin or ["-"])).values(pinned=False))
        if pin:
            res = conn.execute(update(db.stories).where(db.stories.c.slug.in_(pin)).values(pinned=True))
            stats["pinned"] = res.rowcount
        if domains:
            conn.execute(update(db.articles).where(db.articles.c.domain.in_(domains), db.articles.c.status == "published")
                         .values(status="unpublished", reject_reason="moderation: domain"))
        for w in words:
            conn.execute(update(db.stories).where(db.stories.c.headline.ilike(f"%{w}%"), db.stories.c.status.in_(["published", hold.HELD]))
                         .values(status="unpublished"))
    return stats


HEAVY_ARTICLE_COLUMNS = {"content_md", "content_text", "feed_content", "embedding"}


def _full_text(conn, story_rows, art_rows) -> dict[int, str]:
    """Article text for the story pages: the lead article's when it may be shown, otherwise the newest
    article that has showable text (what the page's fullTextArticle picks). Reading the text of every
    article, most of which no page shows, was most of the database's monthly transfer allowance."""
    leads = {s.id: s.lead_article_id for s in story_rows}
    by_story: dict[int, list] = {}
    for a in art_rows:  # newest first, as the page lists them
        by_story.setdefault(a.story_id, []).append(a)
    shown: dict[int, int] = {}  # story id -> article id whose text to read
    fallback: list[int] = []
    for sid, members in by_story.items():
        lead = next((m for m in members if m.id == leads.get(sid)), members[0])
        if lead.show_fulltext and (lead.word_count or 0) > 0:
            shown[sid] = lead.id
        fallback.extend(m.id for m in members if m.show_fulltext and m.id != lead.id)
    texts: dict[int, str] = {}

    def read(ids):
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(db.articles.c.id, db.articles.c.content_md)
                                  .where(db.articles.c.id.in_(ids[i : i + 500]), db.articles.c.content_md.isnot(None))).all():
                if r.content_md:
                    texts[r.id] = r.content_md

    read(list(shown.values()))
    # Stories whose lead has no text: find which other articles have some without reading it, then
    # read only the first one per story.
    missing = {sid for sid in by_story if shown.get(sid) not in texts}
    story_of = {a.id: a.story_id for a in art_rows}
    candidates = [aid for aid in fallback if story_of.get(aid) in missing]
    has_text: set[int] = set()
    for i in range(0, len(candidates), 500):
        has_text.update(r.id for r in conn.execute(select(db.articles.c.id).where(
            db.articles.c.id.in_(candidates[i : i + 500]), db.articles.c.content_md.isnot(None), db.articles.c.content_md != "")).all())
    pick = []
    for sid in missing:
        first = next((m.id for m in by_story[sid] if m.id in has_text), None)
        if first:
            pick.append(first)
    read(pick)
    return texts


def run() -> dict:
    eng = db.engine()
    out_dir = config.SITE_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    now = db.utcnow()
    since = now - timedelta(days=config.EXPORT_DAYS)
    rules = load_moderation()
    moderation = apply_moderation(eng, rules)
    # Risky single-source stories get status "held" (and are released again when a second publisher
    # or an `approve` entry arrives) before the query below, which exports only "published" ones.
    if config.HOLD_RISKY_CLAIMS:
        held, moderation["hold"] = hold.review(eng, since, [s for s in rules.get("approve") or [] if s])
        moderation["hold"].update(hold.write_review(out_dir, held, _iso(now)))
    else:
        # Hold switched off: publish anything still held and drop the review files, so the admin page
        # shows no review card.
        with eng.begin() as conn:
            released = conn.execute(update(db.stories).where(db.stories.c.status == hold.HELD).values(status="published")).rowcount
        for name in ("held.json", "held.enc.json"):
            (out_dir / name).unlink(missing_ok=True)
        moderation["hold"] = {"enabled": False, "released": released or 0}

    with eng.connect() as conn:
        src_rows = conn.execute(select(db.sources)).all()
        sources = {
            s.id: {"key": s.key, "name": s.name, "url": s.url, "kind": s.kind, "type": s.source_type,
                   "weight": s.weight, "discovered": s.discovered, "enabled": s.enabled}
            for s in src_rows
        }
        # Supabase's free plan counts every byte read (5 GB a month) and this runs 48 times a day,
        # so large columns are left behind: embeddings are never exported, and article text is read
        # only for the one article per story whose text the page shows (see _full_text).
        story_rows = conn.execute(
            select(*[c for c in db.stories.c if c.name != "embedding"])
            .where(db.stories.c.status == "published", db.stories.c.updated_at >= since)
            .order_by(db.stories.c.updated_at.desc())
        ).all()
        story_ids = [s.id for s in story_rows]
        art_rows = []
        for i in range(0, len(story_ids), 500):
            chunk = story_ids[i : i + 500]
            art_rows.extend(conn.execute(
                select(*[c for c in db.articles.c if c.name not in HEAVY_ARTICLE_COLUMNS])
                .where(db.articles.c.story_id.in_(chunk), db.articles.c.status == "published")
                .order_by(db.articles.c.published_at.desc())
            ).all())
        full_text = _full_text(conn, story_rows, art_rows)
        sent = {n.date: {"publicUrl": n.public_url, "subject": n.subject} for n in conn.execute(select(db.newsletters)).all()}
        thread_rows = conn.execute(select(db.threads).where(db.threads.c.status == "published")).all()

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
                "contentMd": full_text.get(m.id),
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
                "modelRelease": m.model_release,
                "funding": m.funding,
                "discussion": (
                    {"site": m.discussion_site, "url": m.discussion_url, "points": m.discussion_points}
                    if m.discussion_url else None
                ),
                "trendScore": m.trend_score,
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
            "threadId": s.thread_id,
            "pulse": s.pulse or None,
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

    briefing = build_briefing(stories_out, now)

    # Threads: only those with 2+ stories are worth a page; singletons stay invisible.
    by_thread: dict[int, list[dict]] = {}
    for st in stories_out:
        if st["threadId"]:
            by_thread.setdefault(st["threadId"], []).append(st)
    threads_out = []
    for t in thread_rows:
        members = sorted(by_thread.get(t.id, []), key=lambda x: x["firstPublishedAt"] or "")
        if len(members) < 2:
            continue
        lead_story = max(members, key=lambda x: (x["importance"], x["score"]))
        named = (t.named_count or 0) >= 2
        threads_out.append({
            "id": t.id,
            "slug": t.slug,
            "title": t.title if named else lead_story["headline"],
            "summary": t.summary if named else (lead_story.get("whyItMatters") or t.summary),
            "named": named,
            "ogImage": f"/og/thread-{t.slug}.png",
            "category": t.category,
            "categoryName": config.CATEGORIES.get(t.category or "", "AI"),
            "entities": t.entities or {},
            "storyCount": len(members),
            "firstAt": members[0]["firstPublishedAt"],
            "updatedAt": members[-1]["updatedAt"],
            "storyIds": [m["id"] for m in members],
        })
    threads_out.sort(key=lambda x: x["updatedAt"] or "", reverse=True)

    # Trackers: one row per model / funding event, deduplicated across articles.
    models: dict[str, dict] = {}
    funding: dict[str, dict] = {}
    for st in stories_out:
        for a in st["articles"]:
            r = a.get("modelRelease")
            # Only real launches of usable models, once each however the name is spelled (trackers.py).
            if r and r.get("name") and tracker_rules.is_release(r, f"{st['headline']} | {a.get('title') or ''}"):
                k = tracker_rules.model_key(r["name"], r.get("lab"))
                row = models.setdefault(k, {**r, "date": a["publishedAt"], "storySlug": st["slug"], "storyHeadline": st["headline"], "sources": 0})
                row["sources"] += 1
                if (a["publishedAt"] or "") < (row["date"] or ""):
                    row["date"] = a["publishedAt"]
                for f in ("license", "context", "link", "lab"):
                    if not row.get(f) and r.get(f):
                        row[f] = r[f]
            f = a.get("funding")
            if f and f.get("company"):
                k = f"{f['company'].lower()}|{f.get('round')}|{int(f['amount_usd']) if f.get('amount_usd') else ''}"
                row = funding.setdefault(k, {**f, "date": a["publishedAt"], "storySlug": st["slug"], "storyHeadline": st["headline"], "sources": 0})
                row["sources"] += 1
                if not row.get("valuation_usd") and f.get("valuation_usd"):
                    row["valuation_usd"] = f["valuation_usd"]
                if len(f.get("investors") or []) > len(row.get("investors") or []):
                    row["investors"] = f["investors"]
    models = tracker_rules.fold_versions(models)
    trackers = {
        "models": sorted(models.values(), key=lambda r: r["date"] or "", reverse=True),
        "funding": sorted(funding.values(), key=lambda r: r["date"] or "", reverse=True),
    }
    # Hub pages: every entity with two or more stories, plus any model or company that has a
    # tracker row (a fact box makes a page worthwhile even with one story).
    tracked = {r["name"].strip().lower() for r in trackers["models"]} | {r["company"].strip().lower() for r in trackers["funding"]}
    entities_out = sorted(
        (e for e in entity_index.values() if len(e["storyIds"]) >= 2 or e["name"].strip().lower() in tracked),
        key=lambda e: len(e["storyIds"]), reverse=True,
    )

    (out_dir / "threads.json").write_text(json.dumps(threads_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "trackers.json").write_text(json.dumps(trackers, ensure_ascii=False), encoding="utf-8")
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
    return {"stories": len(stories_out), "entities": len(entities_out), "briefing": len(briefing["storyIds"]),
            "threads": len(threads_out), "models": len(trackers["models"]), "funding": len(trackers["funding"]),
            "moderation": moderation, "dir": str(out_dir)}


# Domains whose posts are the primary source of a story regardless of which feed found them.
PRIMARY_DOMAINS = {
    "openai.com", "anthropic.com", "claude.com", "deepmind.google", "blog.google", "research.google",
    "ai.meta.com", "about.fb.com", "blogs.nvidia.com", "nvidia.com", "huggingface.co", "mistral.ai",
    "microsoft.com", "blogs.microsoft.com", "azure.microsoft.com", "aws.amazon.com", "machinelearning.apple.com",
    "x.ai", "cohere.com", "stability.ai", "arxiv.org", "github.com", "deepseek.com", "qwenlm.github.io",
    "ai.google.dev", "cloud.google.com", "apple.com", "meta.com", "perplexity.ai", "cursor.com",
    "europa.eu", "whitehouse.gov", "gov.uk", "nist.gov", "ftc.gov", "sec.gov",
}
