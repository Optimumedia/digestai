"""Step: admin.json for the dashboard. Pipeline health, the article funnel, source health,
model usage, content mix and reader engagement, all from the database, no extra service."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import func, select

from . import config, db

log = logging.getLogger("digest.admin")

DAYS = 14


def _iso(dt):
    dt = db.as_utc(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _day(dt) -> str:
    return (_iso(dt) or "")[:10]


def run() -> dict:
    eng = db.engine()
    now = db.utcnow()
    since = now - timedelta(days=DAYS)
    week = now - timedelta(days=7)
    days = [(now - timedelta(days=i)).date().isoformat() for i in range(DAYS - 1, -1, -1)]
    out: dict = {"generatedAt": _iso(now), "days": days, "dbMode": "postgres" if db.engine().dialect.name != "sqlite" else "sqlite"}

    with eng.connect() as conn:
        # ---- runs: one row per step; the dashboard groups them into pipeline runs.
        runs = conn.execute(select(db.runs).where(db.runs.c.started_at >= since).order_by(db.runs.c.started_at.desc()).limit(600)).all()
        out["runs"] = [{"step": r.step, "startedAt": _iso(r.started_at), "finishedAt": _iso(r.finished_at), "stats": r.stats or {}} for r in runs]

        # ---- funnel per day from article status.
        arts = conn.execute(
            select(db.articles.c.created_at, db.articles.c.status, db.articles.c.reject_reason, db.articles.c.enrich_model,
                   db.articles.c.source_id, db.articles.c.discussion_points, db.articles.c.trend_score, db.articles.c.category)
            .where(db.articles.c.created_at >= since)
        ).all()
        funnel = {d: {"fetched": 0, "rejectedExtract": 0, "rejectedGate": 0, "rejectedLlm": 0, "published": 0, "pending": 0} for d in days}
        by_model: dict[str, dict[str, int]] = {}
        for a in arts:
            d = _day(a.created_at)
            if d not in funnel:
                continue
            f = funnel[d]
            f["fetched"] += 1
            if a.status == "published":
                f["published"] += 1
                m = (a.enrich_model or "heuristic").split(":")[0]
                by_model.setdefault(d, {})[m] = by_model.get(d, {}).get(m, 0) + 1
            elif a.status in ("rejected", "unpublished"):
                r = a.reject_reason or ""
                key = "rejectedExtract" if r.startswith("extract") else "rejectedLlm" if r.startswith("llm") else "rejectedGate"
                f[key] += 1
            else:
                f["pending"] += 1
        out["funnel"] = [{"day": d, **v} for d, v in funnel.items()]
        out["publishedByModel"] = [{"day": d, **by_model.get(d, {})} for d in days]

        # ---- sources with 7-day performance.
        src = conn.execute(select(db.sources)).all()
        per_src: dict[int, dict] = {}
        for a in arts:
            if db.as_utc(a.created_at) < week:
                continue
            p = per_src.setdefault(a.source_id, {"articles": 0, "published": 0, "rejected": 0, "discussed": 0})
            p["articles"] += 1
            if a.status == "published":
                p["published"] += 1
                if (a.discussion_points or 0) > 0 or (a.trend_score or 0) > 0:
                    p["discussed"] += 1
            elif a.status == "rejected":
                p["rejected"] += 1
        out["sources"] = [{
            "key": s.key, "name": s.name, "kind": s.kind, "type": s.source_type, "weight": s.weight,
            "enabled": s.enabled, "discovered": s.discovered, "expiresAt": _iso(s.expires_at),
            "lastFetchedAt": _iso(s.last_fetched_at), "errorCount": s.error_count, "lastError": (s.last_error or "")[:160] or None,
            "performance": round(s.engagement_ema or 0.0, 2),
            **per_src.get(s.id, {"articles": 0, "published": 0, "rejected": 0, "discussed": 0}),
        } for s in src]

        # ---- LLM usage.
        usage = conn.execute(select(db.llm_usage).where(db.llm_usage.c.day >= days[0])).all()
        out["llm"] = {
            "budgets": config.DAILY_BUDGET,
            "usage": [{"day": u.day, "provider": u.provider, "requests": u.requests, "exhausted": u.exhausted} for u in usage],
        }

        # ---- content mix.
        stories = conn.execute(
            select(db.stories.c.id, db.stories.c.slug, db.stories.c.headline, db.stories.c.category, db.stories.c.score,
                   db.stories.c.importance, db.stories.c.article_count, db.stories.c.pinned, db.stories.c.status,
                   db.stories.c.first_published_at, db.stories.c.thread_id, db.stories.c.pulse)
            .where(db.stories.c.updated_at >= since)
        ).all()
        cats: dict[str, int] = {}
        per_day_stories = {d: 0 for d in days}
        for s in stories:
            if s.status != "published":
                continue
            cats[s.category or "other"] = cats.get(s.category or "other", 0) + 1
            d = _day(s.first_published_at)
            if d in per_day_stories:
                per_day_stories[d] += 1
        out["content"] = {
            "categories": [{"key": k, "name": config.CATEGORIES.get(k, k), "stories": v} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])],
            "storiesPerDay": [{"day": d, "stories": n} for d, n in per_day_stories.items()],
            "threads": conn.execute(select(func.count()).select_from(db.threads).where(db.threads.c.story_count >= 2)).scalar() or 0,
            "storiesWithPulse": sum(1 for s in stories if s.pulse),
            "unpublished": sum(1 for s in stories if s.status == "unpublished"),
            "pinned": [s.slug for s in stories if s.pinned],
        }
        top = sorted((s for s in stories if s.status == "published"), key=lambda s: -(s.score or 0))[:40]
        out["topStories"] = [{"slug": s.slug, "headline": s.headline, "category": s.category, "score": round(s.score or 0, 3),
                              "importance": s.importance, "sources": s.article_count, "pinned": s.pinned,
                              "publishedAt": _iso(s.first_published_at)} for s in top]

        # ---- engagement, when the events table has anything.
        ev = conn.execute(
            select(func.date(db.events.c.created_at).label("day"), db.events.c.type, func.count(), func.sum(db.events.c.value),
                   func.count(func.distinct(db.events.c.session)))
            .where(db.events.c.created_at >= since)
            .group_by(func.date(db.events.c.created_at), db.events.c.type)
        ).all()
        per_day_ev: dict[str, dict] = {d: {"day": d, "views": 0, "sessions": 0, "clicks": 0, "saves": 0, "follows": 0, "shares": 0, "dwellSeconds": 0} for d in days}
        for day, etype, n, total, sessions in ev:
            d = str(day)[:10]
            if d not in per_day_ev:
                continue
            row = per_day_ev[d]
            if etype == "view":
                row["views"] += int(n); row["sessions"] = max(row["sessions"], int(sessions or 0))
            elif etype == "click_source":
                row["clicks"] += int(n)
            elif etype == "save":
                row["saves"] += int(n)
            elif etype == "follow":
                row["follows"] += int(n)
            elif etype == "share":
                row["shares"] += int(n)
            elif etype == "dwell":
                row["dwellSeconds"] += int(total or 0)
        has_events = any(r["views"] for r in per_day_ev.values())
        top_engaged = []
        if has_events:
            rows = conn.execute(
                select(db.stories.c.slug, db.stories.c.headline, func.sum(db.articles.c.engagement).label("e"))
                .join(db.articles, db.articles.c.story_id == db.stories.c.id)
                .where(db.stories.c.updated_at >= since)
                .group_by(db.stories.c.id).order_by(func.sum(db.articles.c.engagement).desc()).limit(15)
            ).all()
            top_engaged = [{"slug": r.slug, "headline": r.headline, "engagement": round(float(r.e or 0), 1)} for r in rows]
        out["engagement"] = {"available": has_events, "perDay": list(per_day_ev.values()), "topStories": top_engaged}

    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / "admin.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {"runs": len(out["runs"]), "sources": len(out["sources"]), "engagement": out["engagement"]["available"]}
