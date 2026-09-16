"""Daily history behind the dashboard's period comparisons (day vs day up to year vs year).

Reader events are pruned after 90 days (rank.py), so longer comparisons need their own record:
one row per UTC day in `daily_stats`. Every run recomputes the last few days (late events still
arrive) and fills any day that has no row yet from whatever the database still holds. A metric
is NULL on days it could not be measured (before tracking existed, or events already pruned when
the day was first filled), so the page can tell "zero" from "no data"."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.engine import Engine

from . import config, db

log = logging.getLogger("digest.history")

RECOMPUTE_DAYS = 3  # today and the two days before: late dwell and click events still land there
# rank.py deletes events older than exactly 90 days, so the day at that cut is already partial.
EVENT_RETENTION_DAYS = 89

EVENT_TYPES = {"view": "views", "click_source": "clicks", "save": "saves", "follow": "follows",
               "share": "shares", "listen": "listens", "push_on": "alert_signups", "search": "searches"}
EVENT_COLS = ["sessions", "visitors", "views", "dwell_seconds", "dwell_reads", "clicks", "saves", "follows", "shares", "listens", "alert_signups", "searches"]
CONTENT_COLS = ["stories_published", "articles_published", "articles_fetched"]
GOOGLE_COLS = ["google_clicks", "google_impressions"]
GROUPS = {"events": EVENT_COLS, "content": CONTENT_COLS, "social": ["social_posts"], "runs": ["crashed_steps"]}
METRIC_COLS = EVENT_COLS + CONTENT_COLS + ["social_posts", "crashed_steps"] + GOOGLE_COLS


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


def _day_of(eng: Engine, col):
    """The UTC calendar day of a timestamp, computed in the database. The time zone is a literal
    so Postgres sees the same expression in SELECT and GROUP BY."""
    if eng.dialect.name == "postgresql":
        return func.date(func.timezone(literal_column("'UTC'"), col))
    return func.date(col)  # SQLite stores naive UTC


def _midnight(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time.min, tzinfo=timezone.utc)


def _days(first: str, last: str) -> list[str]:
    d, end, out = date.fromisoformat(first), date.fromisoformat(last), []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _first_day(conn, col) -> str | None:
    v = conn.execute(select(func.min(col))).scalar()
    if v is None:
        return None
    if isinstance(v, str):
        return v[:10]
    return db.as_utc(v).date().isoformat()


def google_days(path: Path | None = None) -> dict[str, tuple[int, int]]:
    """(clicks, impressions) per day from the gsc.json the gsc step wrote this run. Search
    Console leaves out days without data, so days inside its range count as zero; days after its
    end (the reporting lag) are left out, so they stay unknown."""
    p = path or config.SITE_DATA_DIR / "gsc.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("history") or data.get("perDay") or []
    got = {r["day"]: (int(r.get("clicks") or 0), int(r.get("impressions") or 0)) for r in rows if r.get("day")}
    if not got:
        return {}
    last = max(data.get("end") or "", max(got))
    return {d: got.get(d, (0, 0)) for d in _days(min(got), last)}


def update(eng: Engine, now: datetime | None = None, google: dict[str, tuple[int, int]] | None = None) -> list[dict]:
    """Upsert the recent and missing days, then return the whole history for admin.json."""
    now = db.as_utc(now or db.utcnow())
    today = now.date().isoformat()
    google = google_days() if google is None else google
    t = db.daily_stats

    with eng.connect() as conn:
        existing = {r.day: r._asdict() for r in conn.execute(select(t)).all()}
        first_event = _first_day(conn, db.events.c.created_at)
        cutoff = (now - timedelta(days=EVENT_RETENTION_DAYS)).date().isoformat()
        firsts = [x for x in (_first_day(conn, db.articles.c.created_at), _first_day(conn, db.stories.c.first_published_at)) if x]
        starts = {
            # Days before the first event had no tracking; days before the cutoff lost events.
            "events": max(first_event, cutoff) if first_event else None,
            "content": min(firsts) if firsts else None,
            "social": _first_day(conn, db.social_posts.c.created_at),
            "runs": _first_day(conn, db.runs.c.started_at),
        }
        begin = min([s for s in starts.values() if s] + ([min(google)] if google else []), default=None)
        if begin is None or begin > today:
            return _export(conn)

        recent = {(now - timedelta(days=i)).date().isoformat() for i in range(RECOMPUTE_DAYS)}
        compute = sorted(d for d in _days(begin, today) if d not in existing or d in recent)
        agg: dict[str, dict] = {}

        def put(day, col: str, value) -> None:
            agg.setdefault(str(day)[:10], {})[col] = value

        if compute:
            hi = _midnight(today) + timedelta(days=1)

            def lo(group: str) -> datetime:
                return _midnight(max(compute[0], starts[group]))

            if starts["events"] and compute[-1] >= starts["events"]:
                e = db.events.c
                d_ev = _day_of(eng, e.created_at)
                for day, etype, n, total, sessions in conn.execute(
                    select(d_ev, e.type, func.count(), func.sum(e.value), func.count(func.distinct(e.session)))
                    .where(e.created_at >= lo("events"), e.created_at < hi)
                    .group_by(d_ev, e.type)
                ).all():
                    if etype == "view":
                        put(day, "views", int(n))
                        put(day, "sessions", int(sessions or 0))
                    elif etype == "dwell":
                        put(day, "dwell_seconds", round(float(total or 0), 1))
                    elif etype in EVENT_TYPES:
                        put(day, EVENT_TYPES[etype], int(n))
                # Visitors: distinct visitor numbers among views; events from before visitor numbers
                # existed count one per session.
                for day, n in conn.execute(
                    select(d_ev, func.count(func.distinct(func.coalesce(e.visitor, e.session))))
                    .where(e.created_at >= lo("events"), e.created_at < hi, e.type == "view")
                    .group_by(d_ev)
                ).all():
                    put(day, "visitors", int(n or 0))
                # Reading sessions: distinct (session, story) pairs that reported time on page.
                for day, n in conn.execute(
                    select(d_ev, func.count(func.distinct(e.session + "|" + func.cast(e.story_id, db.String))))
                    .where(e.created_at >= lo("events"), e.created_at < hi, e.type == "dwell")
                    .group_by(d_ev)
                ).all():
                    put(day, "dwell_reads", int(n or 0))

            if starts["content"] and compute[-1] >= starts["content"]:
                a = db.articles.c
                d_art = _day_of(eng, a.created_at)
                for day, n, published in conn.execute(
                    select(d_art, func.count(), func.sum(case((a.status == "published", 1), else_=0)))
                    .where(a.created_at >= lo("content"), a.created_at < hi)
                    .group_by(d_art)
                ).all():
                    put(day, "articles_fetched", int(n))
                    put(day, "articles_published", int(published or 0))
                s = db.stories.c
                d_st = _day_of(eng, s.first_published_at)
                for day, n in conn.execute(
                    select(d_st, func.count())
                    .where(s.first_published_at >= lo("content"), s.first_published_at < hi, s.status == "published")
                    .group_by(d_st)
                ).all():
                    put(day, "stories_published", int(n))

            if starts["social"] and compute[-1] >= starts["social"]:
                p = db.social_posts.c
                d_so = _day_of(eng, p.created_at)
                for day, n in conn.execute(
                    select(d_so, func.count()).where(p.created_at >= lo("social"), p.created_at < hi).group_by(d_so)
                ).all():
                    put(day, "social_posts", int(n))

            if starts["runs"] and compute[-1] >= starts["runs"]:
                r = db.runs.c
                crashed: dict[str, int] = {}
                for started, stats in conn.execute(
                    select(r.started_at, r.stats).where(r.started_at >= lo("runs"), r.started_at < hi)
                ).all():
                    if isinstance(stats, dict) and stats.get("crashed"):
                        d = db.as_utc(started).date().isoformat()
                        crashed[d] = crashed.get(d, 0) + 1
                for d, n in crashed.items():
                    put(d, "crashed_steps", n)

    rows = []
    for d in compute:
        got, old = agg.get(d, {}), existing.get(d, {})
        row: dict = {"day": d, "updated_at": now}
        for group, cols in GROUPS.items():
            measured = starts[group] is not None and d >= starts[group]
            for c in cols:
                # Unmeasured now: keep whatever an earlier run recorded for that day.
                row[c] = got.get(c, 0) if measured else old.get(c)
        if d in google:
            row["google_clicks"], row["google_impressions"] = google[d]
        else:
            row["google_clicks"], row["google_impressions"] = old.get("google_clicks"), old.get("google_impressions")
        rows.append(row)

    # Search Console revises recent days and covers ~16 months: refresh older stored days too.
    compute_set = set(compute)
    fixes = [(d, c, i) for d, (c, i) in google.items()
             if d not in compute_set and d in existing
             and (existing[d]["google_clicks"], existing[d]["google_impressions"]) != (c, i)]

    # Delete and insert in one transaction: portable between SQLite and Postgres.
    with eng.begin() as conn:
        for k in range(0, len(compute), 500):
            conn.execute(t.delete().where(t.c.day.in_(compute[k:k + 500])))
        if rows:
            conn.execute(t.insert(), rows)
        for d, c, i in fixes:
            conn.execute(t.update().where(t.c.day == d).values(google_clicks=c, google_impressions=i, updated_at=now))
    log.info("daily history: %d days written, %d Search Console days corrected", len(rows), len(fixes))

    with eng.connect() as conn:
        return _export(conn)


def _export(conn) -> list[dict]:
    t = db.daily_stats
    return [{"day": r.day, **{_camel(c): getattr(r, c) for c in METRIC_COLS}}
            for r in conn.execute(select(t).order_by(t.c.day)).all()]
