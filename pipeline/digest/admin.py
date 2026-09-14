"""Step: admin.json for the dashboard. Pipeline health, the article funnel, source health,
model usage, content mix and reader engagement, all from the database, no extra service."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import func, select

from . import config, db, history

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

    # ---- daily history for the Compare section; a failure here must not cost the rest of the page.
    history_error = None
    try:
        out["history"] = history.update(eng, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("daily history failed: %s", str(exc)[:200])
        history_error = str(exc)[:120]
        out["history"] = []

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
        per_day_ev: dict[str, dict] = {d: {"day": d, "views": 0, "sessions": 0, "clicks": 0, "saves": 0, "follows": 0, "shares": 0, "dwellSeconds": 0, "dwellReads": 0} for d in days}
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
        # Reading sessions: distinct (session, story) pairs that reported any time on page.
        for day, n in conn.execute(
            select(func.date(db.events.c.created_at), func.count(func.distinct(db.events.c.session + "|" + func.cast(db.events.c.story_id, db.String))))
            .where(db.events.c.created_at >= since, db.events.c.type == "dwell")
            .group_by(func.date(db.events.c.created_at))
        ).all():
            d = str(day)[:10]
            if d in per_day_ev:
                per_day_ev[d]["dwellReads"] = int(n or 0)
        has_events = any(r["views"] for r in per_day_ev.values())
        top_engaged = []
        if has_events:
            rows = conn.execute(
                select(db.stories.c.slug, db.stories.c.headline, func.sum(db.articles.c.engagement).label("e"))
                .join(db.articles, db.articles.c.story_id == db.stories.c.id)
                .where(db.stories.c.updated_at >= since, db.stories.c.status == "published")
                .group_by(db.stories.c.id).order_by(func.sum(db.articles.c.engagement).desc()).limit(15)
            ).all()
            top_engaged = [{"slug": r.slug, "headline": r.headline, "engagement": round(float(r.e or 0), 1)} for r in rows]
        out["engagement"] = {"available": has_events, "perDay": list(per_day_ev.values()), "topStories": top_engaged}

        # ---- story performance: prediction vs what actually happened (readers, or the web).
        per_story_ev: dict[int, dict] = {}
        if has_events:
            for sid, etype, n, total in conn.execute(
                select(db.events.c.story_id, db.events.c.type, func.count(), func.sum(db.events.c.value))
                .where(db.events.c.created_at >= since, db.events.c.story_id.isnot(None))
                .group_by(db.events.c.story_id, db.events.c.type)
            ).all():
                row = per_story_ev.setdefault(sid, {"views": 0, "dwell": 0.0, "dwellN": 0, "clicks": 0, "saves": 0, "shares": 0})
                if etype == "view": row["views"] += int(n)
                elif etype == "dwell": row["dwell"] += float(total or 0); row["dwellN"] += int(n)
                elif etype == "click_source": row["clicks"] += int(n)
                elif etype == "save": row["saves"] += int(n)
                elif etype == "share": row["shares"] += int(n)
        art_by_story: dict[int, list] = {}
        for a in conn.execute(
            select(db.articles.c.story_id, db.articles.c.predicted_score, db.articles.c.engagement, db.articles.c.discussion_points,
                   db.articles.c.trend_score, db.articles.c.published_at, db.articles.c.created_at, db.articles.c.source_id)
            .where(db.articles.c.status == "published", db.articles.c.created_at >= week)
        ).all():
            art_by_story.setdefault(a.story_id, []).append(a)
        src_type = {s.id: s.source_type for s in src}
        perf = []
        for s in stories:
            if s.status != "published" or s.id not in art_by_story:
                continue
            members = art_by_story[s.id]
            ev_row = per_story_ev.get(s.id, {})
            pop = max((float(m.discussion_points or 0) for m in members), default=0.0)
            trend = max((float(m.trend_score or 0) for m in members), default=0.0)
            perf.append({
                "slug": s.slug, "headline": s.headline, "category": s.category, "score": round(s.score or 0, 3),
                "importance": s.importance, "sources": s.article_count, "pinned": s.pinned,
                "publishedAt": _iso(s.first_published_at),
                "primary": any(src_type.get(m.source_id) == "primary" for m in members),
                "predicted": round(max((m.predicted_score or 0.0) for m in members), 2),
                "actual": round(sum((m.engagement or 0.0) for m in members), 1),
                "hnPoints": int(pop), "trend": int(trend),
                "views": ev_row.get("views", 0),
                "dwellAvg": round(ev_row["dwell"] / ev_row["dwellN"]) if ev_row.get("dwellN") else None,
                "clicks": ev_row.get("clicks", 0), "saves": ev_row.get("saves", 0), "shares": ev_row.get("shares", 0),
            })
        perf.sort(key=lambda p: (-(p["actual"] if has_events else (p["hnPoints"] + p["trend"])), -p["score"]))
        out["performance"] = perf[:60]

        # ---- freshness: minutes from publication to appearing in our database.
        lat_rows = conn.execute(
            select(db.articles.c.created_at, db.articles.c.published_at, db.articles.c.source_id)
            .where(db.articles.c.created_at >= since, db.articles.c.published_at.isnot(None), db.articles.c.status == "published")
        ).all()
        by_day_lat: dict[str, list[float]] = {d: [] for d in days}
        by_src_lat: dict[int, list[float]] = {}
        for r in lat_rows:
            mins = (db.as_utc(r.created_at) - db.as_utc(r.published_at)).total_seconds() / 60
            if mins < 0 or mins > 7 * 24 * 60:
                continue
            d = _day(r.created_at)
            if d in by_day_lat:
                by_day_lat[d].append(mins)
            if db.as_utc(r.created_at) >= week:
                by_src_lat.setdefault(r.source_id, []).append(mins)

        def median(xs: list[float]):
            if not xs:
                return None
            xs = sorted(xs)
            return round(xs[len(xs) // 2])

        out["freshness"] = {
            "perDay": [{"day": d, "medianMinutes": median(v), "articles": len(v)} for d, v in by_day_lat.items()],
            "perSource": {str(sid): median(v) for sid, v in by_src_lat.items()},
        }
        for s_out in out["sources"]:
            sid = next((s.id for s in src if s.key == s_out["key"]), None)
            s_out["medianMinutes"] = median(by_src_lat.get(sid, []))

        # ---- extraction quality per publisher domain, 7 days vs last 24 hours.
        ex_rows = conn.execute(
            select(db.articles.c.domain, db.articles.c.extraction_method, db.articles.c.status, db.articles.c.reject_reason,
                   db.articles.c.created_at, db.articles.c.show_fulltext)
            .where(db.articles.c.created_at >= week, db.articles.c.status != "new")
        ).all()
        day_ago = now - timedelta(days=1)
        quality: dict[str, dict] = {}
        for r in ex_rows:
            q = quality.setdefault(r.domain, {"domain": r.domain, "full": 0, "short": 0, "description": 0, "failed": 0, "total": 0, "recentTotal": 0, "recentBad": 0})
            m = r.extraction_method or ""
            if (r.reject_reason or "").startswith("extract"):
                cls = "failed"
            elif m.endswith("-short"):
                cls = "short"
            elif m == "description" or m == "none":
                cls = "description"
            else:
                cls = "full"
            q[cls] += 1
            q["total"] += 1
            if db.as_utc(r.created_at) >= day_ago:
                q["recentTotal"] += 1
                if cls in ("failed", "description"):
                    q["recentBad"] += 1
        qual = sorted(quality.values(), key=lambda q: -q["total"])[:25]
        for q in qual:
            q["fullRate"] = round(q["full"] / q["total"], 2) if q["total"] else None
            q["recentBadRate"] = round(q["recentBad"] / q["recentTotal"], 2) if q["recentTotal"] >= 3 else None
        out["extraction"] = qual

        # ---- alerts: the things that need a human today.
        alerts = []
        last_steps = [r for r in runs if db.as_utc(r.started_at) >= now - timedelta(hours=1)]
        if any((r.stats or {}).get("crashed") for r in last_steps):
            alerts.append({"level": "critical", "text": "The latest pipeline run had a crashed step. Open the Actions log."})
        if not runs or db.as_utc(runs[0].started_at) < now - timedelta(hours=2):
            alerts.append({"level": "critical", "text": "No pipeline run in the last 2 hours. The schedule may be paused."})
        for s in out["sources"]:
            if s["enabled"] and not s["discovered"] and s["errorCount"] >= 12:
                alerts.append({"level": "warning", "text": f"Source {s['name']} has failed {s['errorCount']} runs in a row: {s['lastError'] or 'no detail'}"})
        for u in usage:
            if u.day == now.date().isoformat() and u.exhausted and now.hour < 12:
                alerts.append({"level": "warning", "text": f"{u.provider} hit its daily quota before noon UTC; the rest of the day runs on fallbacks."})
        recent_pub = conn.execute(select(func.count()).select_from(db.stories).where(db.stories.c.first_published_at >= now - timedelta(hours=3), db.stories.c.status == "published")).scalar() or 0
        if recent_pub == 0 and now.hour not in (2, 3, 4, 5):
            alerts.append({"level": "warning", "text": "No new story published in the last 3 hours."})
        for q in qual:
            if q["recentBadRate"] is not None and q["recentBadRate"] >= 0.6 and (q["fullRate"] or 0) >= 0.5:
                alerts.append({"level": "warning", "text": f"Extraction from {q['domain']} is failing today ({int(q['recentBadRate']*100)}% bad) after working this week; the site may have changed."})
        if history_error:
            alerts.append({"level": "warning", "text": f"The daily history behind Compare could not be updated: {history_error}"})
        out["alerts"] = alerts

    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / "admin.json").write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return {"runs": len(out["runs"]), "sources": len(out["sources"]), "engagement": out["engagement"]["available"], "historyDays": len(out["history"])}
