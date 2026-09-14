"""Step: admin.json for the dashboard. Pipeline health, the article funnel, source health,
model usage, content mix and reader engagement, all from the database, no extra service."""
from __future__ import annotations

import re

import json
import logging
import os
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import func, select

from . import config, db, history
from . import quality as content_quality

log = logging.getLogger("digest.admin")

DAYS = 14



# Traffic sources as people know them. Matched on the utm_source tag or the referring host.
_SOURCE_NAMES = [
    (("bluesky", "bsky.app", "bsky.social"), "Bluesky"),
    (("news.ycombinator.com", "hackernews", "hn"), "Hacker News"),
    (("reddit.com", "old.reddit.com", "reddit"), "Reddit"),
    (("linkedin.com", "lnkd.in", "linkedin"), "LinkedIn"),
    (("t.co", "x.com", "twitter.com", "twitter", "x"), "X"),
    (("facebook.com", "l.facebook.com", "m.facebook.com", "facebook"), "Facebook"),
    (("producthunt.com", "producthunt"), "Product Hunt"),
    (("newsletter", "kit", "email"), "Newsletter"),
    (("push", "alert"), "Browser alerts"),
    (("podcast",), "Podcast"),
]


def source_name(raw: str | None) -> str:
    """'direct', a utm_source tag or a referring host, as a readable name."""
    s = (raw or "").strip().lower()
    if s.startswith("www."):
        s = s[4:]
    if not s or s == "direct" or s.endswith("digestai.news"):
        return "Direct"
    for keys, name in _SOURCE_NAMES:
        if s in keys or any("." in k and (s == k or s.endswith("." + k)) for k in keys):
            return name
    if re.match(r"^(?:[a-z0-9-]+\.)*google(?:\.[a-z]{2,3}){1,2}$", s) or s == "google":
        return "Google"
    if re.match(r"^(?:[a-z0-9-]+\.)*bing\.com$", s) or s == "bing":
        return "Bing"
    if "duckduckgo" in s:
        return "DuckDuckGo"
    return s

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
        per_day_ev: dict[str, dict] = {d: {"day": d, "views": 0, "sessions": 0, "visitors": 0, "clicks": 0, "saves": 0, "follows": 0, "shares": 0, "dwellSeconds": 0, "dwellReads": 0} for d in days}
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
        # Visitors: distinct visitor numbers among views (one per session for older events).
        for day, n in conn.execute(
            select(func.date(db.events.c.created_at), func.count(func.distinct(func.coalesce(db.events.c.visitor, db.events.c.session))))
            .where(db.events.c.created_at >= since, db.events.c.type == "view")
            .group_by(func.date(db.events.c.created_at))
        ).all():
            d = str(day)[:10]
            if d in per_day_ev:
                per_day_ev[d]["visitors"] = int(n or 0)
        # Where visitors came from and which pages they opened, last 7 days.
        since7 = db.utcnow() - timedelta(days=7)
        who = func.coalesce(db.events.c.visitor, db.events.c.session)
        # Grouped per visitor, so one person arriving as "bluesky" and "bsky.app" counts once.
        by_source: dict[str, dict] = {}
        for raw, visitor_key, n_views in conn.execute(
            select(db.events.c.source, who, func.count())
            .where(db.events.c.created_at >= since7, db.events.c.type == "view")
            .group_by(db.events.c.source, who)
        ).all():
            row = by_source.setdefault(source_name(raw), {"name": source_name(raw), "who": set(), "views": 0})
            row["who"].add(visitor_key); row["views"] += int(n_views or 0)
        sources7 = sorted(({"name": r["name"], "visitors": len(r["who"]), "views": r["views"]} for r in by_source.values()),
                          key=lambda r: (-r["visitors"], -r["views"]))
        pages7 = [{"path": p or "/", "views": int(n), "visitors": int(v or 0)} for p, n, v in conn.execute(
            select(db.events.c.path, func.count(), func.count(func.distinct(who)))
            .where(db.events.c.created_at >= since7, db.events.c.type == "view")
            .group_by(db.events.c.path).order_by(func.count().desc()).limit(10)
        ).all()]
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
        out["engagement"] = {"available": has_events, "perDay": list(per_day_ev.values()), "topStories": top_engaged,
                             "sources7": sources7, "pages7": pages7}

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

        # ---- action cards: the things that need a human, each with why it matters and what to do.
        step_rows = [{"step": r.step, "startedAt": db.as_utc(r.started_at), "stats": r.stats or {}} for r in runs]
        actions = run_cards(step_rows, now)
        if not runs or db.as_utc(runs[0].started_at) < now - timedelta(hours=2):
            actions.append(_card("pipeline:silent", "critical", "No pipeline run has started in the last 2 hours.",
                                 "The site only updates when the pipeline runs, so no new stories are appearing.",
                                 "Start a run now. If nothing starts, the schedule may be paused in GitHub Actions.",
                                 action={"kind": "run", "label": "Run pipeline now"}))
        for s in out["sources"]:
            if s["enabled"] and not s["discovered"] and s["errorCount"] >= 12:
                actions.append(_card(f"source:{s['key']}", "warning", f"{s['name']} has failed to load {s['errorCount']} runs in a row.",
                                     "No articles arrive from it while it fails. The site keeps working, but every run wastes time retrying it.",
                                     "If it is still failing tomorrow, pause it. You can switch it back on later in sources.yaml.",
                                     detail=f"Last error: {s['lastError']}" if s["lastError"] else None,
                                     action={"kind": "pause", "source": s["key"], "name": s["name"], "label": "Pause this source"}))
        for u in usage:
            if u.day == now.date().isoformat() and u.exhausted and now.hour < 12:
                actions.append(_card(f"quota:{u.provider}", "info", f"{u.provider.title()} used up its daily allowance before noon (UTC).",
                                     "The rest of today's summaries are written by the backup models, which are a little less polished.",
                                     "No action needed; the allowance resets at midnight UTC."))
        recent_pub = conn.execute(select(func.count()).select_from(db.stories).where(db.stories.c.first_published_at >= now - timedelta(hours=3), db.stories.c.status == "published")).scalar() or 0
        if recent_pub == 0 and now.hour not in (2, 3, 4, 5):
            actions.append(_card("pipeline:quiet", "warning", "No new story has been published in the last 3 hours.",
                                 "Returning readers find the same front page, which makes the site look abandoned.",
                                 "Check the latest run in the Actions log. Quiet hours happen at night, but three hours in the daytime is unusual.",
                                 action={"kind": "link", "url": ACTIONS_URL, "label": "Open the Actions log"}))
        for q in qual:
            if q["recentBadRate"] is not None and q["recentTotal"] >= 5 and q["recentBadRate"] >= 0.6 and (q["fullRate"] or 0) >= 0.5:
                actions.append(_card(f"extract:{q['domain']}", "info", f"Articles from {q['domain']} could not be read in full today ({q['recentBad']} of {q['recentTotal']}).",
                                     "Its stories show only a short summary until this works again; the publisher probably changed its pages.",
                                     "No action needed unless it lasts several days."))
        if history_error:
            actions.append(_card("history", "info", "The daily history behind Compare could not be updated.",
                                 "The Compare numbers may miss today until the next successful run.",
                                 "No action needed unless it repeats.", detail=history_error))
        actions += search_cards(_read_json("gsc.json"), step_rows, now)

        # ---- content quality: sample older story pages that should still be online.
        cut_hi, cut_lo = now - timedelta(days=2), now - timedelta(days=60)
        older = conn.execute(select(db.stories.c.slug, db.stories.c.headline)
                             .where(db.stories.c.status == "published", db.stories.c.first_published_at < cut_hi, db.stories.c.first_published_at >= cut_lo)
                             .order_by(db.stories.c.first_published_at.desc())).all()
        step = max(1, len(older) // content_quality.MAX_STORY_PAGES)
        own_pages = [{"slug": r.slug, "headline": r.headline} for r in older[::step]][:content_quality.MAX_STORY_PAGES]
        out["storyTimes"] = sorted(_iso(s.first_published_at) for s in stories if s.status == "published" and s.first_published_at)

    try:
        out["quality"] = content_quality.run(now, own_pages=own_pages)
        actions += out["quality"]["cards"]
    except Exception as exc:  # noqa: BLE001  (a quality check must never cost the dashboard)
        log.warning("quality checks failed: %s", str(exc)[:200])
        out["quality"] = {"checkedAt": _iso(now), "error": str(exc)[:120], "flags": {}, "cards": []}

    order = {"critical": 0, "warning": 1, "info": 2}
    actions.sort(key=lambda c: order.get(c["level"], 3))
    out["actions"] = actions
    # alerts: the older flat list notify.py turns into a GitHub issue; information cards stay out.
    out["alerts"] = [{"level": c["level"], "text": _plain(c)} for c in actions if c["level"] in ("critical", "warning")]

    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / "admin.json").write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return {"runs": len(out["runs"]), "sources": len(out["sources"]), "engagement": out["engagement"]["available"], "historyDays": len(out["history"]),
            "actions": len(actions), "qualitySeconds": out["quality"].get("seconds")}


# ---------------------------------------------------------------------------- action cards

ACTIONS_URL = f"https://github.com/{os.environ.get('GITHUB_REPOSITORY') or 'Optimumedia/digestai'}/actions/workflows/pipeline.yml"
FAILED_RUNS_URL = ACTIONS_URL + "?query=is%3Afailure"

# What each step does, in words for someone who never reads the code.
STEP_WORDS = {
    "fetch": "collecting new articles", "extract": "reading the articles", "gate": "filtering out off-topic articles",
    "enrich": "writing summaries", "cluster": "grouping articles into stories", "threads": "linking related stories",
    "discuss": "checking online discussions", "pulse": "summing up community reactions", "rank": "ranking stories",
    "export": "preparing the stories for the site", "push": "sending browser alerts", "topics": "updating topic pages",
    "intros": "writing topic introductions", "images": "making share pictures", "audio": "recording the audio briefing",
    "social": "posting to social media", "newsletter": "sending the newsletter", "gsc": "reading Google Search data",
    "admin": "updating this dashboard", "notify": "raising alerts", "indexnow": "notifying search engines",
}


def _card(cid: str, level: str, what: str, why: str, todo: str, at=None, detail: str | None = None,
          action: dict | None = None, items: list | None = None) -> dict:
    """what may contain {at}; the page shows that time in the reader's own time zone."""
    card = {"id": cid, "level": level, "what": what, "why": why, "todo": todo}
    if at is not None:
        card["at"] = _iso(at)
    if detail:
        card["detail"] = detail[:200]
    if action:
        card["action"] = action
    if items:
        card["items"] = items
    return card


def _plain(card: dict) -> str:
    at = card.get("at")
    return card["what"].replace("{at}", f"{at[11:16]} UTC" if at else "an earlier time")


def _read_json(name: str):
    try:
        return json.loads((config.SITE_DATA_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def group_runs(rows: list[dict]) -> list[list[dict]]:
    """Step rows into pipeline runs, oldest first: a run starts at fetch or after a 29-minute gap."""
    groups: list[list[dict]] = []
    for r in sorted(rows, key=lambda r: r["startedAt"]):
        if groups and r["step"] != "fetch" and r["startedAt"] - groups[-1][0]["startedAt"] < timedelta(minutes=29):
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


def run_cards(rows: list[dict], now) -> list[dict]:
    """Failed runs in the last 24 hours, translated. A crashed step makes the workflow stop before
    the site is built, so readers keep the previous version: one failure is information, a streak
    is an emergency."""
    groups = [g for g in group_runs(rows) if g[0]["startedAt"] >= now - timedelta(hours=24)]
    crashed = lambda g: next((s for s in g if (s["stats"] or {}).get("crashed")), None)  # noqa: E731
    failed = [(g, crashed(g)) for g in groups if crashed(g)]
    if not failed:
        return []
    streak = 0
    for g in reversed(groups):
        if not crashed(g):
            break
        streak += 1
    _, step = failed[-1]
    doing = STEP_WORDS.get(step["step"], f"the {step['step']} step")
    link = {"kind": "link", "url": FAILED_RUNS_URL, "label": "Open the Actions log"}
    if streak >= 2:
        return [_card("runs:failing", "critical", f"The last {streak} runs failed, the latest at {{at}} while {doing}.",
                      "While runs fail the site is not updated, so readers see no new stories.",
                      "Open the Actions log to see the error, then run the pipeline again. If it fails again it needs a fix in the code.",
                      at=step["startedAt"], action=link)]
    if streak == 1:
        return [_card("runs:latest", "warning", f"The latest run failed at {{at}} while {doing}.",
                      "The site kept the previous version, so readers saw no problem, but new stories wait for the next run.",
                      "No action needed unless the next run fails too.", at=step["startedAt"], action=link)]
    if len(failed) >= 3:
        return [_card("runs:repeated", "warning", f"{len(failed)} runs failed in the last 24 hours, the latest at {{at}} while {doing}.",
                      "Each failed run keeps the previous version of the site, so new stories arrive later than they should.",
                      "Open the Actions log. Failures that keep coming back usually need a fix.", at=step["startedAt"], action=link)]
    what = f"A run failed at {{at}} while {doing}." if len(failed) == 1 else f"2 runs failed in the last 24 hours, the latest at {{at}} while {doing}."
    return [_card("runs:earlier", "info", what,
                  "The site kept the previous version and later runs worked, so readers saw no problem.",
                  "No action needed unless it repeats.", at=step["startedAt"], action=link)]


def search_cards(gsc: dict | None, rows: list[dict], now) -> list[dict]:
    """Google indexing and Search Console freshness. Needs at least 20 submitted pages before
    calling zero indexed a problem."""
    if not gsc:
        return []
    out = []
    maps = gsc.get("sitemaps") or []
    # A sitemap index repeats the pages of the sitemaps it lists: the largest one, not the sum.
    submitted = max((int(m.get("submitted") or 0) for m in maps), default=0)
    indexed = sum(int(m.get("indexed") or 0) for m in maps)
    if submitted >= 20 and indexed == 0:
        prop = gsc.get("property") or f"sc-domain:{config.SITE_URL.split('//', 1)[-1]}"
        inspect = f"https://search.google.com/search-console/inspect?resource_id={quote(prop, safe='')}&id={quote(config.SITE_URL + '/', safe='')}"
        errors = [{"headline": m.get("path"), "detail": f"This sitemap entry reports {m.get('errors')} error(s); if it is a typo, remove it in Search Console."}
                  for m in maps if str(m.get("errors") or "0") not in ("0", "")]
        out.append(_card("search:indexed", "warning", f"Google has indexed 0 of {submitted} submitted pages.",
                         "Pages Google has not indexed cannot appear in its results, so search brings no visitors. New sites often wait a few weeks, but a nudge helps.",
                         "In Search Console, inspect the home page and one recent story, then press \"Request indexing\" for each.",
                         action={"kind": "link", "url": inspect, "label": "Inspect in Search Console"}, items=errors or None))
    gsc_steps = sorted((r for r in rows if r["step"] == "gsc" and (r["stats"] or {}).get("configured")), key=lambda r: r["startedAt"])
    ok = [r for r in gsc_steps if not r["stats"].get("error") and not r["stats"].get("crashed")]
    if gsc_steps and (not ok or ok[-1]["startedAt"] < now - timedelta(hours=26)):
        what = "Google Search numbers have not refreshed since {at}." if ok else "Google Search numbers have not refreshed in the last two weeks."
        out.append(_card("search:stale", "warning", what,
                         "The Search tab and the Google figures in Compare are out of date, so they can hide a drop or a rise.",
                         "Usually the Search Console connection lost access or its key expired. The Actions log shows the error on the \"gsc\" step.",
                         at=ok[-1]["startedAt"] if ok else None, action={"kind": "link", "url": ACTIONS_URL, "label": "Open the Actions log"}))
    return out
