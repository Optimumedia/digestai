"""Step: admin.json for the dashboard. Pipeline health, the article funnel, source health,
model usage, content mix and reader engagement, all from the database, no extra service."""
from __future__ import annotations

import re

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import and_, case, func, or_, select

from . import cache, config, db, history, share
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
    (("instagram.com", "instagram", "ig"), "Instagram"),
    (("threads.net", "threads.com", "threads"), "Threads"),
    (("youtube.com", "youtu.be", "youtube"), "YouTube"),
    (("tiktok.com", "tiktok"), "TikTok"),
    (("mastodon.social", "mastodon.online", "mstdn.social", "mas.to", "fosstodon.org", "hachyderm.io",
      "infosec.exchange", "sigmoid.social", "techhub.social", "mastodon"), "Mastodon"),
    (("t.me", "telegram.org", "telegram.me", "telegram"), "Telegram"),
    (("discord.com", "discordapp.com", "discord.gg", "discord"), "Discord"),
    (("slack.com", "slack"), "Slack"),
    (("whatsapp.com", "wa.me", "whatsapp"), "WhatsApp"),
]
# Android apps report themselves as the referrer ("android-app://com.instagram.android").
_APP_NAMES = {"com.instagram": "Instagram", "com.linkedin": "LinkedIn", "com.reddit": "Reddit", "com.twitter": "X",
              "com.facebook": "Facebook", "xyz.blueskyweb": "Bluesky", "org.telegram": "Telegram", "com.whatsapp": "WhatsApp",
              "com.discord": "Discord", "com.slack": "Slack", "com.google.android.youtube": "YouTube",
              "com.zhiliaoapp.musically": "TikTok", "org.joinmastodon": "Mastodon", "com.google.android.gm": "Gmail",
              "com.google.android.googlequicksearchbox": "Google"}


_TZ: dict | None = None


def country_of(zone: str | None) -> str | None:
    """ISO country code for a browser time zone (tz database zone.tab plus older names browsers still
    report); None for UTC, Etc/* and anything unknown."""
    global _TZ
    if _TZ is None:
        _TZ = json.loads((Path(__file__).with_name("tz_countries.json")).read_text(encoding="utf-8"))
    return _TZ["zones"].get((zone or "").strip())


def country_name(code: str | None) -> str:
    if not code:
        return "Unknown"
    if _TZ is None:
        country_of(None)
    return _TZ["names"].get(code, code)


def countries_summary(rows) -> list[dict]:
    """rows: (time zone, views, visitors) per zone. Visitors in two zones of one country (rare) count twice."""
    by: dict[str, dict] = {}
    for zone, views, visitors in rows:
        code = country_of(zone)
        key = code or ""
        row = by.setdefault(key, {"code": code, "name": country_name(code), "views": 0, "visitors": 0})
        row["views"] += int(views or 0)
        row["visitors"] += int(visitors or 0)
    return sorted(by.values(), key=lambda r: (r["code"] is None, -r["visitors"], -r["views"]))


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
    for prefix, name in _APP_NAMES.items():
        if s == prefix or s.startswith(prefix + "."):
            return name
    if re.match(r"^(?:[a-z0-9-]+\.)*(?:mastodon|mstdn)(?:[.-][a-z0-9-]+)*\.[a-z]{2,}$", s):
        return "Mastodon"
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
        # A finished step's row never changes, so the runner's copy (cache.py) serves all but the newest.
        run_ids = conn.execute(
            select(db.runs.c.id, case((db.runs.c.finished_at.isnot(None), 1), else_=0).label("done"))
            .where(db.runs.c.started_at >= since).order_by(db.runs.c.started_at.desc()).limit(600)).all()
        got = cache.RUNS.get(conn, {r.id: [r.done] for r in run_ids})
        runs = [got[r.id] for r in run_ids if r.id in got]
        out["runs"] = [{"step": r.step, "startedAt": _iso(r.started_at), "finishedAt": _iso(r.finished_at), "stats": r.stats or {}} for r in runs]

        # ---- funnel per day from article status (the runner's copy of recent articles).
        mirror = cache.articles(conn)
        arts = sorted((a for a in mirror.values() if db.as_utc(a.created_at) >= since), key=lambda a: a.id)
        funnel = {d: {"fetched": 0, "rejectedExtract": 0, "rejectedGate": 0, "rejectedLlm": 0, "published": 0, "pending": 0} for d in days}
        by_model: dict[str, dict[str, int]] = {}
        for a in arts:
            d = _day(a.created_at)
            if d not in funnel:
                continue
            f = funnel[d]
            f["fetched"] += 1
            if a.status in ("published", "overflow"):  # overflow: coverage of a full story (cluster.py)
                f["published"] += 1
                m = (a.enrich_model or "heuristic").split(":")[0]
                by_model.setdefault(d, {})[m] = by_model.get(d, {}).get(m, 0) + 1
            elif a.status in ("rejected", "unpublished"):
                r = a.reject_kind or ""
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
            if a.status in ("published", "overflow"):
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
        story_mirror = sorted((s for s in cache.stories(conn).values() if db.as_utc(s.updated_at) >= since), key=lambda s: s.id)
        titles = cache.story_titles(conn, story_mirror)
        stories = [cache.merged(s, titles.get(s.id)) for s in story_mirror if s.id in titles]
        for s in stories:
            s.pulse = (s.len_pulse or 0) > 0  # only whether there is one
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
            # Time on page and read depth are story reading here; /work pages send them without a
            # story (their own line: work_engagement).
            .where(db.events.c.created_at >= since,
                   or_(db.events.c.type.notin_(("dwell", "depth")), db.events.c.story_id.isnot(None)))
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
        # Counted in the database per referrer; only a name several referrers share ("bluesky" and
        # "bsky.app") needs a second count across them, so no visitor list is read.
        by_raw = conn.execute(
            select(db.events.c.source, func.count(), func.count(func.distinct(who)))
            .where(db.events.c.created_at >= since7, db.events.c.type == "view")
            .group_by(db.events.c.source)
        ).all()
        by_source: dict[str, dict] = {}
        for raw, n_views, n_who in by_raw:
            row = by_source.setdefault(source_name(raw), {"name": source_name(raw), "raws": [], "visitors": 0, "views": 0})
            row["raws"].append(raw); row["views"] += int(n_views or 0); row["visitors"] = int(n_who or 0)
        for row in by_source.values():
            if len(row["raws"]) > 1:
                named = [r for r in row["raws"] if r is not None]
                cond = db.events.c.source.in_(named) if named else db.events.c.source.is_(None)
                if len(named) < len(row["raws"]):
                    cond = cond | db.events.c.source.is_(None)
                row["visitors"] = int(conn.execute(
                    select(func.count(func.distinct(who)))
                    .where(db.events.c.created_at >= since7, db.events.c.type == "view", cond)).scalar() or 0)
        sources7 = sorted(({"name": r["name"], "visitors": r["visitors"], "views": r["views"]} for r in by_source.values()),
                          key=lambda r: (-r["visitors"], -r["views"]))
        pages7 = [{"path": p or "/", "views": int(n), "visitors": int(v or 0)} for p, n, v in conn.execute(
            select(db.events.c.path, func.count(), func.count(func.distinct(who)))
            .where(db.events.c.created_at >= since7, db.events.c.type == "view")
            .group_by(db.events.c.path).order_by(func.count().desc()).limit(10)
        ).all()]
        # Where readers are: the country of each visit's time zone setting, 7 days.
        countries7 = countries_summary(conn.execute(
            select(db.events.c.tz, func.count(), func.count(func.distinct(who)))
            .where(db.events.c.created_at >= since7, db.events.c.type == "view")
            .group_by(db.events.c.tz)
        ).all())
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
                             "sources7": sources7, "pages7": pages7, "countries7": countries7}

        # ---- site searches and how far stories are read.
        out["engagement"]["searches7"] = search_summary(conn.execute(
            select(db.events.c.detail, db.events.c.value, who, db.events.c.created_at)
            .where(db.events.c.created_at >= since7, db.events.c.type == "search")
        ).all())
        depth_rows = conn.execute(
            select(db.events.c.session, db.events.c.story_id, db.events.c.value, db.events.c.detail, db.events.c.created_at)
            .where(db.events.c.created_at >= since, db.events.c.type == "depth", db.events.c.story_id.isnot(None))
        ).all()
        out["engagement"]["depth7"] = depth_summary([r[:4] for r in depth_rows if db.as_utc(r[4]) >= since7])
        depth_by_story = depth_summary([r[:4] for r in depth_rows])["perStory"]

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
        for a in arts:
            if a.status == "published" and db.as_utc(a.created_at) >= week:
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
                "depthAvg": depth_by_story.get(s.id),
            })
        perf.sort(key=lambda p: (-(p["actual"] if has_events else (p["hnPoints"] + p["trend"])), -p["score"]))
        out["performance"] = perf[:60]

        # ---- freshness: minutes from publication to appearing in our database.
        lat_rows = [a for a in arts if a.published_at is not None and a.status == "published"]
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
        ex_rows = [a for a in arts if db.as_utc(a.created_at) >= week and a.status != "new"]
        day_ago = now - timedelta(days=1)
        quality: dict[str, dict] = {}
        for r in ex_rows:
            q = quality.setdefault(r.domain, {"domain": r.domain, "full": 0, "short": 0, "description": 0, "failed": 0, "total": 0, "recentTotal": 0, "recentBad": 0})
            m = r.extraction_method or ""
            if (r.reject_kind or "").startswith("extract"):
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
        # Silent after two scheduled runs were missed (at least two hours).
        silent_hours = max(2, -(-5 * config.RUN_INTERVAL_MINUTES // 120))  # ceil(2.5 intervals)
        if not runs or db.as_utc(runs[0].started_at) < now - timedelta(hours=silent_hours):
            actions.append(_card("pipeline:silent", "critical", f"No pipeline run has started in the last {silent_hours} hours.",
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
        # One model running out of its free allowance is normal (the others take over); only a stop in
        # summaries, which holds new stories back, is worth a card.
        waiting, oldest_waiting = conn.execute(
            select(func.count(), func.min(db.articles.c.created_at)).where(db.articles.c.status == "gated")).one()
        exhausted_today = {u.provider for u in usage if u.day == now.date().isoformat() and u.exhausted}
        actions += summary_cards(step_rows, exhausted_today, int(waiting or 0), db.as_utc(oldest_waiting), now)
        actions += check_cards(step_rows, now)
        actions += upgrade_cards(step_rows, now)
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
        actions += site_search_cards(out["engagement"]["searches7"])
        # AI at Work (/work): its health from the files the export and audio steps just wrote; no read.
        out["work"] = work_summary(_read_json("work.json"), _read_json("work-briefing.json"),
                                   _read_json("work-episodes.json"), step_rows, now)
        if out["work"] is not None:
            out["work"]["engagement7"] = work_engagement(conn, now - timedelta(days=7))
        actions += work_health_cards(out["work"], now)

        # ---- the database against the free plan: its size, and how much the pipeline reads.
        try:
            size = db.database_size(conn)
        except Exception as exc:  # noqa: BLE001 - a size query must never cost the dashboard
            log.warning("database size failed: %s", str(exc)[:200])
            size = None
        out["database"] = database_summary(size, step_rows, out["history"], now, out["dbMode"])
        out["schedule"] = {"runsPerDay": config.RUNS_PER_DAY, "intervalMinutes": config.RUN_INTERVAL_MINUTES,
                           "runMinutes": config.RUN_MINUTES}
        actions += database_cards(out["database"])
        out["storage"] = storage_summary()
        actions += storage_cards(out["storage"], now)

        # ---- content quality: sample older story pages that should still be online.
        cut_hi, cut_lo = now - timedelta(days=2), now - timedelta(days=60)
        older = sorted((s for s in cache.stories(conn).values()
                        if s.status == "published" and cut_lo <= db.as_utc(s.first_published_at) < cut_hi),
                       key=lambda s: (db.as_utc(s.first_published_at), s.id), reverse=True)
        step = max(1, len(older) // content_quality.MAX_STORY_PAGES)
        sample = older[::step][:content_quality.MAX_STORY_PAGES]
        sample_titles = cache.story_titles(conn, sample)
        own_pages = [{"slug": sample_titles[r.id].slug, "headline": sample_titles[r.id].headline} for r in sample if r.id in sample_titles]
        out["storyTimes"] = sorted(_iso(s.first_published_at) for s in stories if s.status == "published" and s.first_published_at)

    try:
        out["quality"] = content_quality.run(now, own_pages=own_pages)
        actions += out["quality"]["cards"]
    except Exception as exc:  # noqa: BLE001  (a quality check must never cost the dashboard)
        log.warning("quality checks failed: %s", str(exc)[:200])
        out["quality"] = {"checkedAt": _iso(now), "error": str(exc)[:120], "flags": {}, "cards": []}

    # ---- Share today: the day's stories for LinkedIn and X with posts ready to paste (share.py). Built
    # from the files the export step wrote and pipeline/digest/shared.json; no database read.
    try:
        out["share"] = share.run_from_files(now)
    except Exception as exc:  # noqa: BLE001 - a shortlist must never cost the dashboard
        log.warning("share shortlist failed: %s", str(exc)[:200])
        out["share"] = None

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
    "upgrade": "improving the summaries of stories that matter",
    "export": "preparing the stories for the site", "push": "sending browser alerts", "topics": "updating topic pages",
    "intros": "writing topic introductions", "images": "making share pictures", "audio": "recording the audio briefing",
    "social": "posting to social media", "newsletter": "sending the newsletter", "gsc": "reading Google Search data",
    "admin": "updating this dashboard", "morning": "writing the morning note", "notify": "raising alerts", "indexnow": "notifying search engines",
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


def work_summary(work: dict | None, briefing: dict | None, episodes: list | None, rows: list[dict], now) -> dict | None:
    """AI at Work in numbers for the Today tab: this week's cards and tools, the jobs with too little
    behind them for their page to be indexed, the directory's gaps (no cost, no official link), what
    the rules kept out, and where the weekly episode stands. Built from work.json, work-briefing.json,
    work-episodes.json and the audio step's own stats: no database read."""
    if not work:
        return None
    from . import work as work_rules  # the section's own rules: week keys, job names, thresholds

    week = work_rules.week_key(now.isoformat())
    bucket = (work.get("weeks") or {}).get(week) or {}
    tools = work.get("tools") or []
    jobs = work.get("jobs") or {}
    # A job page is indexed from three items (site/src/lib/indexing.mjs, JOB_MIN_ITEMS).
    thin = [work_rules.JOBS[j] for j in work_rules.JOBS if len(jobs.get(j) or []) < 3]
    empty = [work_rules.JOBS[j] for j in work_rules.JOBS if not jobs.get(j)]
    audio = next((r["stats"].get("work") for r in rows if r["step"] == "audio" and isinstance(r["stats"].get("work"), dict)), None)
    latest = (episodes or [None])[0] if episodes else None
    last_week = work_rules.week_key((now - timedelta(days=7)).isoformat())
    last_bucket = (work.get("weeks") or {}).get(last_week) or {}
    return {
        "week": week,
        "cards": len(bucket.get("changed") or []),
        "tools": bucket.get("tools") or 0,
        "try": bucket.get("tryCount", len(bucket.get("try") or [])),
        "skip": bucket.get("skipCount", len(bucket.get("skip") or [])),
        "total": len(work.get("storyIds") or []),
        "toolCount": len(tools),
        "noCost": sum(1 for t in tools if (t.get("costKind") or "unknown") == "unknown"),
        "noLink": sum(1 for t in tools if not t.get("link")),
        "thinJobs": thin,
        "emptyJobs": empty,
        "recent": (briefing or {}).get("stats", {}).get("items", 0),
        "windowHours": (briefing or {}).get("windowHours", 24),
        "dropped": {why: d.get("count", 0) for why, d in (work.get("dropped") or {}).items()},
        "droppedTools": sorted({t for d in (work.get("dropped") or {}).values() for t in d.get("tools") or []})[:8],
        "episode": {"week": latest.get("week"), "date": latest.get("date"), "seconds": latest.get("seconds")} if latest else None,
        "lastWeek": last_week,
        "lastWeekItems": last_bucket.get("tryCount", len(last_bucket.get("try") or [])),
        "lastWeekEpisode": any(e.get("week") == last_week for e in episodes or []),
        "audioReason": (audio or {}).get("reason") or None,
        "minItems": config.WORK_AUDIO_MIN_ITEMS,
    }


def work_engagement(conn, since) -> dict:
    """How readers use AI at Work since `since`: views and average read depth of the /work pages,
    and the card actions (try, copy_prompt, expand, next_click) wherever a card is shown. One grouped
    query returning at most six rows, so it costs no egress to speak of."""
    e = db.events.c
    rows = conn.execute(
        select(e.type, func.count(), func.avg(e.value))
        .where(e.created_at >= since,
               or_(e.type.in_(db.WORK_EVENT_TYPES),
                   and_(e.type.in_(("view", "depth")), or_(e.path == "/work", e.path.like("/work/%")))))
        .group_by(e.type)
    ).all()
    return work_engagement_summary(rows)


def work_engagement_summary(rows) -> dict:
    """(type, count, average value) rows into the admin line's numbers."""
    by = {str(t): (int(n or 0), float(avg or 0)) for t, n, avg in rows}
    depth_n, depth_avg = by.get("depth", (0, 0.0))
    return {
        "views": by.get("view", (0, 0.0))[0],
        "depthAvg": round(depth_avg) if depth_n else None,
        "depthReads": depth_n,
        "tries": by.get("try", (0, 0.0))[0],
        "copies": by.get("copy_prompt", (0, 0.0))[0],
        "expands": by.get("expand", (0, 0.0))[0],
        "nextClicks": by.get("next_click", (0, 0.0))[0],
    }


def work_health_cards(w: dict | None, now) -> list[dict]:
    """Two things about AI at Work worth a card, and only these: the section has stopped getting new
    items, or last week's episode is late. The rest is a line on the Today tab, not a to-do."""
    if not w:
        return []
    out = []
    if w["recent"] == 0 and w["windowHours"] >= 48:
        out.append(_card("work:quiet", "warning", "AI at Work has had no new item for two days.",
                         "The section's front page, its job pages and the AI at Work block on the home page stop changing, "
                         "and it is one of the site's three focus areas.",
                         "Open the latest run's enrich step: \"work_cards\" is how many practical cards the summaries found, "
                         "\"work_dropped_developer\" how many the rules kept out. Zero of both means the marketing feeds were quiet.",
                         action={"kind": "link", "url": "/work", "label": "Open AI at Work"}))
    # From Tuesday: Monday's runs had every chance to read last week, if last week had enough to read.
    if (now.weekday() >= 1 and not w["lastWeekEpisode"] and w["lastWeekItems"] >= w["minItems"]
            and config.WORK_AUDIO):
        out.append(_card("work:episode", "info", f"The AI at Work episode for {w['lastWeek']} has not been published.",
                         "Podcast subscribers get nothing this week, and the playbook page has no player.",
                         "No action needed if it appears after the next runs; the audio step retries all week.",
                         detail=f"Audio step: {w['audioReason']}" if w.get("audioReason") else None))
    return out


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
    """Google indexing (from URL inspection of the key pages) and Search Console freshness."""
    if not gsc:
        return []
    out = []
    maps = gsc.get("sitemaps") or []
    prop = gsc.get("property") or f"sc-domain:{config.SITE_URL.split('//', 1)[-1]}"

    def inspect_url(page: str) -> str:
        return f"https://search.google.com/search-console/inspect?resource_id={quote(prop, safe='')}&id={quote(config.SITE_URL + page, safe='')}"

    errors = [{"headline": m.get("path"), "detail": f"This sitemap entry reports {m.get('errors')} error(s); if it is a typo, remove it in Search Console."}
              for m in maps if str(m.get("errors") or "0") not in ("0", "")]
    # Google no longer fills in the sitemap report's indexed count (always 0), so the card uses the
    # page-by-page inspection of the key pages and top stories that the gsc step records.
    checks = gsc.get("inspections") or []
    missing = [c for c in checks if not str(c.get("state") or "").lower().startswith(("submitted and indexed", "indexed"))]
    if missing:
        home_missing = any(c.get("page") == "/" for c in missing)
        items = [{"headline": c["page"], "detail": f"Google: {c.get('state')}.",
                  "action": {"kind": "link", "url": inspect_url(c["page"]), "label": "Request indexing"}} for c in missing]
        out.append(_card("search:indexed", "warning" if home_missing else "info",
                         f"Google has indexed {len(checks) - len(missing)} of {len(checks)} key pages checked.",
                         "A page Google has not indexed cannot appear in its results. New sites are indexed a few pages at a time; links from other sites speed it up.",
                         "Open each page below in Search Console and press \"Request indexing\". Google allows only a few requests a day, so start with pages it does not know yet.",
                         items=items + errors))
    elif errors:
        out.append(_card("search:sitemap", "warning", "A sitemap entry in Search Console reports errors.",
                         "Google may skip the pages a broken sitemap entry lists.",
                         "If the entry below is a typo, remove it in Search Console under Indexing, Sitemaps.", items=errors))
    out += ranking_cards(gsc)
    gsc_steps = sorted((r for r in rows if r["step"] == "gsc" and (r["stats"] or {}).get("configured")), key=lambda r: r["startedAt"])
    ok = [r for r in gsc_steps if not r["stats"].get("error") and not r["stats"].get("crashed")]
    if gsc_steps and (not ok or ok[-1]["startedAt"] < now - timedelta(hours=26)):
        what = "Google Search numbers have not refreshed since {at}." if ok else "Google Search numbers have not refreshed in the last two weeks."
        out.append(_card("search:stale", "warning", what,
                         "The Search tab and the Google figures in Compare are out of date, so they can hide a drop or a rise.",
                         "Usually the Search Console connection lost access or its key expired. The Actions log shows the error on the \"gsc\" step.",
                         at=ok[-1]["startedAt"] if ok else None, action={"kind": "link", "url": ACTIONS_URL, "label": "Open the Actions log"}))
    return out


DB_WARN_MB, DB_CRITICAL_MB = 350, 450
READS_WARN_MB_PER_MONTH = 4096


def billing_cycle(now, day: int | None = None) -> tuple:
    """(start, end) dates of the Supabase billing cycle containing `now`; it starts on `day` each month."""
    from datetime import date

    day = day or config.SUPABASE_CYCLE_DAY

    def on(y: int, m: int) -> date:
        import calendar

        return date(y, m, min(day, calendar.monthrange(y, m)[1]))

    today = now.date()
    y, m = today.year, today.month
    start = on(y, m) if today >= on(y, m) else on(y - (m == 1), 12 if m == 1 else m - 1)
    ny, nm = start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1
    return start, on(ny, nm)


def database_summary(size: dict | None, rows: list[dict], history: list[dict], now, mode: str) -> dict:
    """Database size against the free plan, what the pipeline read per step in its latest run, and
    what that comes to over a month and over this billing cycle. Reads are the pipeline's own estimate
    (readKB per step); Supabase's usage page is the authority and also counts anything else."""
    kb = lambda g: sum(float((s["stats"] or {}).get("readKB") or 0) for s in g)  # noqa: E731
    measured = [g for g in group_runs(rows) if any((s["stats"] or {}).get("readKB") is not None for s in g)]
    # The newest group is this run, still going (this step has not recorded its reads yet).
    done = measured[:-1] if len(measured) > 1 else measured
    latest = done[-1] if done else None
    day = [g for g in done if g[0]["startedAt"] >= now - timedelta(hours=24)] or ([latest] if latest else [])
    avg_run_kb = sum(kb(g) for g in day) / len(day) if day else None
    monthly_mb = avg_run_kb * config.RUNS_PER_DAY * 30 / 1024 if avg_run_kb is not None else None
    steps: dict[str, float] = {}
    for s in latest or []:
        if (s["stats"] or {}).get("readKB") is not None:
            steps[s["step"]] = steps.get(s["step"], 0.0) + float(s["stats"]["readKB"])

    start, end = billing_cycle(now)
    in_cycle = [r for r in history if start.isoformat() <= r.get("day", "") <= now.date().isoformat() and r.get("dbReadKb") is not None]
    cycle_mb = sum(float(r["dbReadKb"]) for r in in_cycle) / 1024
    days_left = max(0.0, (datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) - now).total_seconds() / 86400)
    projected = cycle_mb + (monthly_mb / 30 * days_left if monthly_mb is not None else 0.0)
    return {
        "mode": mode,
        "sizeMB": round(size["bytes"] / 1048576, 1) if size else None,
        "limitMB": config.SUPABASE_DB_MB,
        "tables": [{"name": t["name"], "mb": round(t["bytes"] / 1048576, 1)} for t in (size or {}).get("tables", [])],
        "reads": {
            "latestRunAt": _iso(latest[0]["startedAt"]) if latest else None,
            "latestRunKB": round(kb(latest), 1) if latest else None,
            "steps": [{"step": k, "readKB": round(v, 1)} for k, v in sorted(steps.items(), key=lambda kv: -kv[1])],
            "avgRunKB": round(avg_run_kb, 1) if avg_run_kb is not None else None,
            "runsAveraged": len(day),
            "runsPerDay": config.RUNS_PER_DAY,
            "monthlyMB": round(monthly_mb, 1) if monthly_mb is not None else None,
            "quotaMB": config.SUPABASE_EGRESS_GB * 1024,
        },
        "cycle": {
            "start": start.isoformat(), "end": end.isoformat(),
            "measuredMB": round(cycle_mb, 1),
            "measuredSince": in_cycle[0]["day"] if in_cycle else None,
            "projectedMB": round(projected, 1),
        },
    }


def _read_cache_json(*parts: str) -> dict:
    try:
        return json.loads(config.CACHE_DIR.joinpath(*parts).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def storage_summary() -> dict:
    """The media store (media.py) and the nightly backup (backup.py), as their steps left them."""
    media = _read_cache_json("media", "status.json")
    backup = _read_cache_json("backup", "state.json")
    archive_count = None
    try:
        archive_count = len(json.loads((config.SITE_DATA_DIR / "archive.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return {
        "media": {k: media.get(k) for k in ("at", "assets", "mb", "pending", "store", "lastUploadAt", "error")} if media else None,
        "backup": {"lastAt": backup.get("lastAt"), "asset": backup.get("lastAsset"), "mb": backup.get("lastMB"),
                   "rows": backup.get("lastRows"), "reason": backup.get("reason"), "configured": bool(os.environ.get("BACKUP_KEY"))},
        "archivePages": archive_count,
        "releasesUrl": f"https://github.com/{os.environ.get('GITHUB_REPOSITORY') or 'Optimumedia/digestai'}/releases",
    }


def storage_cards(s: dict, now) -> list[dict]:
    out = []
    b = s.get("backup") or {}
    last = datetime.fromisoformat(b["lastAt"]) if b.get("lastAt") else None
    if not b.get("configured"):
        out.append(_card("backup:setup", "info", "The database is not backed up yet.",
                         "Supabase's free plan keeps no copy you can download, so a mistake or a lost project would lose every story.",
                         "Create the BACKUP_KEY secret (SETUP.md, \"Database backup\"). The pipeline then saves an encrypted copy every night."))
    elif last is None or now - last > timedelta(hours=50):
        out.append(_card("backup:stale", "warning", "The nightly database backup has not run for two days." if last else "No database backup has been made yet.",
                         "Without a recent copy, anything lost in the database cannot be restored.",
                         "Open the Actions log and look at the backup step.",
                         detail=f"Last reason: {b.get('reason')}" if b.get("reason") else None,
                         action={"kind": "link", "url": ACTIONS_URL, "label": "Open the Actions log"}))
    m = s.get("media") or {}
    if m.get("store") == "unreachable" or (m.get("pending") or 0) >= 300:
        out.append(_card("media:store", "warning", f"{m.get('pending') or 0} share images, thumbnails or episodes are waiting to be uploaded.",
                         "They are served from the site meanwhile, which uses up its size limit; if the runner's copy is lost they are made again.",
                         "No action needed if it clears within a day. If not, check the media step in the Actions log.",
                         detail=m.get("error")))
    return out


def database_cards(d: dict) -> list[dict]:
    out = []
    size, limit = d.get("sizeMB"), d.get("limitMB") or 500
    if size is not None and size >= DB_WARN_MB:
        critical = size >= DB_CRITICAL_MB
        out.append(_card("database:size", "critical" if critical else "warning",
                         f"The database holds {size:.0f} MB of the {limit:.0f} MB the free plan allows.",
                         "When it is full, Supabase stops the database from accepting new data, so no new stories could be saved.",
                         "Ask for old article text to be cleared sooner (the tidy step), or move to a paid plan."
                         if critical else "No action needed yet. If it keeps growing week after week, ask for old article text to be cleared sooner."))
    reads = d.get("reads") or {}
    monthly, quota = reads.get("monthlyMB"), reads.get("quotaMB") or 5120
    if monthly is not None and monthly >= READS_WARN_MB_PER_MONTH:
        heavy = (reads.get("steps") or [{}])[0]
        out.append(_card("database:reads", "warning",
                         f"At the current rate the pipeline reads about {monthly / 1024:.1f} GB a month from the database; the free plan includes {quota / 1024:.0f} GB.",
                         "Above the allowance Supabase charges for the extra or limits the project.",
                         "Open the Pipeline tab to see which step reads the most. Running the pipeline less often lowers it in proportion.",
                         detail=f"Latest run: {reads.get('latestRunKB', 0) / 1024:.1f} MB, most of it in the {heavy.get('step')} step."
                         if heavy.get("step") else None,
                         action={"kind": "link", "url": "#pipeline", "label": "Open the Pipeline tab"}))
    return out


def summary_cards(rows: list[dict], exhausted_today: set[str], waiting: int, oldest_waiting, now) -> list[dict]:
    """A card only when summaries stop while articles wait: every summary model is out of its daily
    allowance, or several runs in a row wrote nothing. One model running out is normal, the others
    take over."""
    if waiting <= 0 or oldest_waiting is None or oldest_waiting > now - timedelta(hours=1):
        return []
    enrich = sorted((r for r in rows if r["step"] == "enrich" and r["startedAt"] >= now - timedelta(hours=24)
                     and not (r["stats"] or {}).get("crashed")), key=lambda r: r["startedAt"])
    streak = 0
    for r in reversed(enrich):
        st = r["stats"] or {}
        if st.get("enriched") or st.get("rejected"):
            break
        streak += 1
    if not streak:
        return []
    budget = (enrich[-1]["stats"] or {}).get("budget") or {}
    all_out = bool(budget) and all((n or 0) <= 0 or p in exhausted_today for p, n in budget.items())
    articles = f"{waiting} article{'s' if waiting != 1 else ''}"
    if all_out:
        hours = 24 - now.hour
        return [_card("summaries:allowance", "warning" if hours >= 4 else "info",
                      "Every summary model has used up its free allowance for today, so no new summaries are being written.",
                      f"{articles} wait for a summary, and new stories reach the site only once they have one. "
                      f"The allowances come back at midnight UTC, in about {hours} hour{'s' if hours != 1 else ''}.",
                      "No action needed if this is rare. If it happens most days, add a key for another free model, "
                      "or lower how many articles each run summarises so the allowance lasts the whole day.")]
    if streak >= 3:
        return [_card("summaries:stopped", "warning", f"The last {streak} runs wrote no summaries while {articles} waited.",
                      "New stories reach the site only once they have a summary, so the front page is not getting new stories.",
                      "Open the Actions log and look at the \"enrich\" step: it says whether a model key stopped working or the models keep failing.",
                      action={"kind": "link", "url": ACTIONS_URL, "label": "Open the Actions log"})]
    return []


CHECK_ITEMS = 8


def check_cards(rows: list[dict], now) -> list[dict]:
    """What the check before storing caught in the last day (enrich.py and upgrade.py write it into
    their run stats): summaries that used a figure or a name their article does not contain. The
    pipeline has already asked the model again or taken the claim out, so this is a card to read,
    not one to act on - unless it starts happening on most summaries."""
    day = [r for r in rows if r["step"] in ("enrich", "upgrade") and r["startedAt"] >= now - timedelta(hours=24)]
    checked = flagged = retried = edited = 0
    caught: list[dict] = []
    for r in day:
        c = (r["stats"] or {}).get("checks") or {}
        checked += int(c.get("checked") or 0)
        flagged += int(c.get("flagged") or 0)
        retried += int(c.get("retried") or 0)
        edited += int(c.get("edited") or 0)
        for item in (c.get("caught") or []) + ((r["stats"] or {}).get("caught") or []):
            what = ", ".join(f'"{i}"' for i in (item.get("items") or [])[:3]) or "a figure"
            done = {"retry": "The model wrote it again and the second answer checked out.",
                    "edited": "Taken out of the summary before it was stored.",
                    "kept": "Left as written: there was nothing safe to remove."}.get(item.get("fixed"), "")
            caught.append({"headline": (item.get("headline") or "an article")[:120],
                           "detail": f"The {item.get('where') or 'summary'} said {what}, which the article does not. {done}".strip()})
    if not flagged:
        return []
    share = flagged / checked if checked else 0
    level = "warning" if checked >= 20 and share >= 0.25 else "info"
    what = (f"The fact check caught {flagged} summary that used a figure or a name its article does not contain."
            if flagged == 1 else
            f"The fact check caught {flagged} summaries that used figures or names their articles do not contain.")
    why = (f"Every summary is compared with its article before it is stored: {checked} were checked in the last day. "
           f"{retried} were written again by the same model and {edited} had the unsupported part removed, so none of "
           "this reached a page. A rising share means a model is inventing detail.")
    todo = ("Nothing to do while this stays occasional. If it passes a quarter of all summaries, the model writing "
            "them is the problem: check which one the enrich step is using in the Actions log.")
    return [_card("summaries:checked", level, what, why, todo, items=caught[:CHECK_ITEMS])]


def upgrade_cards(rows: list[dict], now) -> list[dict]:
    """A card only when the second pass has been unable to run for a day while stories were
    waiting for it: the free allowance never got ahead of pace."""
    day = [r for r in rows if r["step"] == "upgrade" and r["startedAt"] >= now - timedelta(hours=24)]
    if not day or any((r["stats"] or {}).get("upgraded") for r in day):
        return []
    latest = (day[-1]["stats"] or {})
    if latest.get("off") or not latest.get("candidates"):
        return []
    return [_card("summaries:upgrades", "info",
                  f"{latest['candidates']} stories are waiting for a better summary and none was written in the last day.",
                  "Stories that turn out to matter are summarised again with the strongest free model, and from all "
                  "their sources when they have several. That has not happened today, because the free allowance was "
                  "never ahead of what the rest of the day needs.",
                  "No action needed. If it lasts several days, either fewer articles need summarising each run, or "
                  "another free model key would pay for itself here.")]


# ---------------------------------------------------------------------------- site searches and read depth

_EMAIL = re.compile(r"\S+@\S+")
_LONG_NUMBER = re.compile(r"\d{5,}")
DEPTH_STAGES = list(db.DEPTH_STAGES)  # top, summary, full_text, end


def clean_query(raw: str | None) -> str:
    """A site search as a subject: lowercased, single spaces, no e-mail addresses or long numbers.
    The browser and the database strip those too; this also merges "GPT-5 " with "gpt-5"."""
    q = _LONG_NUMBER.sub(" ", _EMAIL.sub(" ", (raw or "").lower()))
    return " ".join(q.split())[:100]


def search_summary(rows) -> dict:
    """rows: (query, results, visitor, created_at). The most searched subjects, and those whose
    latest search found nothing: what readers want that the site does not cover."""
    by_q: dict[str, dict] = {}
    total = 0
    for query, results, who, at in rows:
        q = clean_query(query)
        if len(q) < 2:
            continue
        total += 1
        row = by_q.setdefault(q, {"searches": 0, "who": set(), "results": 0, "at": None})
        row["searches"] += 1
        row["who"].add(who)
        at = db.as_utc(at)
        if row["at"] is None or (at is not None and at >= row["at"]):
            row["at"], row["results"] = at, int(results or 0)
    items = sorted(({"query": q, "searches": r["searches"], "visitors": len(r["who"]), "results": r["results"]} for q, r in by_q.items()),
                   key=lambda r: (-r["visitors"], -r["searches"], r["query"]))
    missing = [r for r in items if r["results"] == 0]
    return {"total": total, "queries": len(items), "noResults": sum(r["searches"] for r in missing),
            "top": items[:15], "missing": missing[:15]}


def depth_summary(rows) -> dict:
    """rows: (session, story_id, percent, stage). A page view can report more than once (each time
    the reader leaves the tab after getting further), so a session counts once per story, at its
    deepest point."""
    deepest: dict[tuple, tuple[float, int]] = {}
    for session, story_id, value, stage in rows:
        rank = DEPTH_STAGES.index(stage) if stage in DEPTH_STAGES else 0
        pct = max(0.0, min(100.0, float(value or 0)))
        old = deepest.get((session, story_id))
        deepest[(session, story_id)] = (max(pct, old[0]), max(rank, old[1])) if old else (pct, rank)
    stages = {s: 0 for s in DEPTH_STAGES}
    per_story: dict[int, list[float]] = {}
    for (_session, story_id), (pct, rank) in deepest.items():
        stages[DEPTH_STAGES[rank]] += 1
        per_story.setdefault(story_id, []).append(pct)
    reads = len(deepest)
    return {"reads": reads, "stages": stages,
            "avgPercent": round(sum(p for p, _ in deepest.values()) / reads) if reads else None,
            "perStory": {sid: round(sum(v) / len(v)) for sid, v in per_story.items()}}


def site_search_cards(summary: dict | None) -> list[dict]:
    """Searches on the site that found nothing, once more than one visitor has tried the same words."""
    missing = [m for m in (summary or {}).get("missing", []) if m["visitors"] >= 2]
    if not missing:
        return []
    items = [{"headline": m["query"], "detail": f"{m['searches']} searches by {m['visitors']} visitors in the last 7 days found nothing."}
             for m in missing[:8]]
    what = (f"Readers searched the site for \"{missing[0]['query']}\" and found nothing." if len(missing) == 1
            else f"Readers searched the site for {len(missing)} subjects it has no story on.")
    return [_card("searches:missing", "info", what,
                  "These are subjects people came looking for. A search that finds nothing is a reader who may leave.",
                  "If a subject belongs on the site, check the Sources tab for a source that covers it, or whether stories use other words for it. "
                  "If it is off-topic or a typo, no action needed.", items=items)]


RANKING_MIN_IMPRESSIONS = 50  # per week, both weeks: below it one search swings the average
RANKING_MIN_MOVE = 3.0  # places


def ranking_cards(gsc: dict | None) -> list[dict]:
    """An info card when the average Google position moved notably between the last 7 reported
    days and the 7 before, with enough impressions on both sides to trust the move."""
    from .gsc import weighted_position

    days = [r for r in (gsc or {}).get("perDay") or [] if r.get("day")]
    if len(days) < 14:
        return []
    week, before = days[-7:], days[-14:-7]
    imp_now, imp_before = sum(r.get("impressions") or 0 for r in week), sum(r.get("impressions") or 0 for r in before)
    now, prev = weighted_position(week), weighted_position(before)
    if now is None or prev is None or min(imp_now, imp_before) < RANKING_MIN_IMPRESSIONS:
        return []
    move = prev - now  # positive: the site moved up
    if abs(move) < max(RANKING_MIN_MOVE, prev * 0.15):
        return []

    def page(p: float) -> str:
        return "the first page" if p <= 10 else f"page {int(-(-p // 10))}"

    what = (f"Google ranks the site higher this week: average position {now:.1f}, up from {prev:.1f}." if move > 0
            else f"Google ranks the site lower this week: average position {now:.1f}, down from {prev:.1f}.")
    why = (f"On average the site now appears on {page(now)} of Google's results, against {page(prev)} the week before "
           f"({imp_now} impressions this week, {imp_before} the week before). 1 is the top result.")
    todo = ("No action needed. The Search tab shows which searches moved." if move > 0
            else "Open the Search tab to see which searches and pages dropped; a drop in one popular search often explains it.")
    return [_card("search:ranking", "info", what, why, todo, action={"kind": "link", "url": "#seo", "label": "Open the Search tab"})]
