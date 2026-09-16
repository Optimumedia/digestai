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

from . import cache, config, db

log = logging.getLogger("digest.history")

RECOMPUTE_DAYS = 3  # today and the two days before: late dwell and click events still land there
# rank.py deletes events older than exactly 90 days, so the day at that cut is already partial.
EVENT_RETENTION_DAYS = 89

EVENT_TYPES = {"view": "views", "click_source": "clicks", "save": "saves", "follow": "follows",
               "share": "shares", "listen": "listens", "push_on": "alert_signups", "search": "searches"}
EVENT_COLS = ["sessions", "visitors", "views", "dwell_seconds", "dwell_reads", "clicks", "saves", "follows", "shares", "listens", "alert_signups", "searches"]
CONTENT_COLS = ["stories_published", "articles_published", "articles_fetched"]
GOOGLE_COLS = ["google_clicks", "google_impressions", "google_position", "google_position_sum", "google_queries"]
GROUPS = {"events": EVENT_COLS, "content": CONTENT_COLS, "social": ["social_posts"], "runs": ["crashed_steps"]}
METRIC_COLS = EVENT_COLS + CONTENT_COLS + ["social_posts", "crashed_steps"] + GOOGLE_COLS + ["db_read_kb"]


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


def google_days(path: Path | None = None) -> dict[str, tuple]:
    """(clicks, impressions, position, queries) per day from the gsc.json the gsc step wrote this
    run. Search Console leaves out days without data, so days inside its range count as zero;
    days after its end (the reporting lag) are left out, so they stay unknown. Position and
    queries are None where the file does not say (older files, days outside the query window)."""
    p = path or config.SITE_DATA_DIR / "gsc.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("history") or data.get("perDay") or []
    got = {r["day"]: (int(r.get("clicks") or 0), int(r.get("impressions") or 0), r.get("position"), r.get("queries"))
           for r in rows if r.get("day")}
    if not got:
        return {}
    last = max(data.get("end") or "", max(got))
    return {d: got.get(d, (0, 0, None, 0)) for d in _days(min(got), last)}


def google_values(value: tuple, old: dict | None = None) -> dict:
    """The daily_stats Google columns for one day. `value` is (clicks, impressions) or
    (clicks, impressions, position, queries); a None position or query count on a day with
    impressions means "not reported", so the stored value is kept. With no impressions the day
    has no position at all (NULL, never 0) and contributes nothing to a period's average."""
    old = old or {}
    clicks, imp, pos, queries = (tuple(value) + (None, None))[:4]
    row = {"google_clicks": clicks, "google_impressions": imp}
    if not imp:
        row.update(google_position=None, google_position_sum=0.0, google_queries=0)
        return row
    if pos is None:
        # Keep a stored position only if it belongs to the same impressions.
        same = old.get("google_impressions") == imp and old.get("google_position") is not None
        row.update(google_position=old.get("google_position") if same else None,
                   google_position_sum=old.get("google_position_sum") if same else None)
    else:
        row.update(google_position=round(float(pos), 2), google_position_sum=round(float(pos) * imp, 2))
    row["google_queries"] = int(queries) if queries is not None else old.get("google_queries")
    return row


def update(eng: Engine, now: datetime | None = None, google: dict[str, tuple] | None = None) -> list[dict]:
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
                read_kb: dict[str, float] = {}
                # Finished step rows never change: the runner's copy (cache.py) has all but the newest.
                ids = conn.execute(select(r.id, case((r.finished_at.isnot(None), 1), else_=0))
                                   .where(r.started_at >= lo("runs"), r.started_at < hi)).all()
                for row in cache.RUNS.get(conn, {i: [done] for i, done in ids}).values():
                    stats = row.stats
                    if not isinstance(stats, dict):
                        continue
                    d = db.as_utc(row.started_at).date().isoformat()
                    if stats.get("crashed"):
                        crashed[d] = crashed.get(d, 0) + 1
                    if stats.get("readKB") is not None:
                        read_kb[d] = read_kb.get(d, 0.0) + float(stats["readKB"])
                for d, n in crashed.items():
                    put(d, "crashed_steps", n)
                for d, kb in read_kb.items():
                    put(d, "db_read_kb", round(kb, 1))

    rows = []
    for d in compute:
        got, old = agg.get(d, {}), existing.get(d, {})
        row: dict = {"day": d, "updated_at": now}
        for group, cols in GROUPS.items():
            measured = starts[group] is not None and d >= starts[group]
            for c in cols:
                # Unmeasured now: keep whatever an earlier run recorded for that day.
                row[c] = got.get(c, 0) if measured else old.get(c)
        row.update(google_values(google[d], old) if d in google else {c: old.get(c) for c in GOOGLE_COLS})
        # Database reads the pipeline measured that day (runs record readKB since 16 Sep 2026); empty before.
        row["db_read_kb"] = got.get("db_read_kb", old.get("db_read_kb"))
        rows.append(row)

    # Search Console revises recent days and covers ~16 months: refresh older stored days too
    # (this also fills in positions for days stored before positions were recorded).
    compute_set = set(compute)
    fixes = []
    for d, value in google.items():
        if d not in compute_set and d in existing:
            new = google_values(value, existing[d])
            if any(existing[d].get(c) != v for c, v in new.items()):
                fixes.append((d, new))

    # Delete and insert in one transaction: portable between SQLite and Postgres.
    with eng.begin() as conn:
        for k in range(0, len(compute), 500):
            conn.execute(t.delete().where(t.c.day.in_(compute[k:k + 500])))
        if rows:
            conn.execute(t.insert(), rows)
        for d, new in fixes:
            conn.execute(t.update().where(t.c.day == d).values(**new, updated_at=now))
    log.info("daily history: %d days written, %d Search Console days corrected", len(rows), len(fixes))

    with eng.connect() as conn:
        return _export(conn)


def _export(conn) -> list[dict]:
    t = db.daily_stats
    return [{"day": r.day, **{_camel(c): getattr(r, c) for c in METRIC_COLS}}
            for r in conn.execute(select(t).order_by(t.c.day)).all()]
