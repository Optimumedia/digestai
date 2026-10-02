"""Readers and likely automated visitors: the one definition every step uses.

Automated browsers open one page each and leave. Their views are recorded like any other (nothing
is ever deleted), so every count of readers has to leave them out the same way: the Readers tab and
the AI at Work line (admin.py), the daily history (history.py), the morning note (morning.py) and
the reader engagement the ranking learns from (rank.py). This module is that rule, once.

The rule. A view is *likely automated* when all three hold:

1. it is the visitor's only event: no second page, no time on page, no read depth, no click, no
   card seen (a visitor is the random per-day number the browser sends, else the tab's session);
2. it came direct: no referrer and no utm_source;
3. its time zone is *bot-heavy*: at least BOT_ZONE_MIN visitors in the zone did only that, and they
   are at least BOT_ZONE_SHARE of all the zone's visitors, over the last 7 or the last 30 days.

Each alone is common among readers (a reader opens the home page once and leaves; a reader in
Singapore comes direct). Together they are what a wave looks like: on 2 Oct 2026, 898 of 1,084
weekly visitors were one direct view each from China, Singapore, Hong Kong and one US zone. The
zone test is by the data, not by a list of countries, and it is what keeps a reader in Warsaw who
opened one page from being dropped. A visit with no time zone (older events, a browser that hides
it) is never marked. Two windows because the counts live on: the 7-day window catches a wave as it
starts, the 30-day one (the ranking's window) keeps its visits out after it has stopped.

Egress: bot_zones() is one grouped query, a row per time zone with four counts, read at most once
every REFRESH_HOURS hours and kept in the runner cache (cache.BOT_ZONES). tagged() sends nothing by
itself: it is a subquery the callers group, so the database still returns a row per day, per zone,
per page or per article, never a row per visit.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import and_, case, func, literal_column, select

from . import cache, db

log = logging.getLogger("digest.readers")

BOT_ZONE_MIN = 5
BOT_ZONE_SHARE = 0.8
ZONE_WINDOWS_DAYS = (7, 30)
REFRESH_HOURS = 1.0
# "The visitor's only event" is judged a day either side of the period asked for, so a reader whose
# view falls just inside it and whose reading falls just outside is not taken for a one-view visit.
# A visitor number lives one UTC day, so a day is always enough.
EDGE = timedelta(days=1)


def _who(e):
    return func.coalesce(e.visitor, e.session)


def bot_heavy(visitors: int, bare: int) -> bool:
    """Whether a zone with `visitors` visitors, `bare` of them one direct view and nothing else, is bot-heavy."""
    return bare >= BOT_ZONE_MIN and bare >= BOT_ZONE_SHARE * visitors


def zone_rows(conn, now) -> list[tuple]:
    """Per time zone: (zone, visitors, bare visitors) over the longer window, then the same two over
    the shorter one. A bare visitor's only event is one view that came direct. One grouped query;
    the database returns a row per zone, not a row per visitor."""
    e = db.events.c
    short, long = min(ZONE_WINDOWS_DAYS), max(ZONE_WINDOWS_DAYS)
    who = _who(e)
    per_visitor = (
        select(who.label("who"), func.max(e.tz).label("tz"), func.count().label("n"),
               func.sum(case((e.type == "view", 1), else_=0)).label("views"),
               func.max(func.coalesce(e.source, "direct")).label("src"),
               func.sum(case((e.created_at >= now - timedelta(days=short), 1), else_=0)).label("recent"))
        .where(e.created_at >= now - timedelta(days=long))
        .group_by(who)
    ).subquery()
    v = per_visitor.c
    bare = and_(v.n == 1, v.views == 1, v.src == "direct")
    recent = v.recent > 0
    return [tuple(r) for r in conn.execute(
        select(v.tz, func.count(), func.sum(case((bare, 1), else_=0)),
               func.sum(case((recent, 1), else_=0)), func.sum(case((and_(bare, recent), 1), else_=0)))
        .where(v.views > 0, v.tz.isnot(None))
        .group_by(v.tz)
    ).all()]


def zones_of(rows) -> list[str]:
    """The bot-heavy zones among zone_rows(): by either window."""
    return sorted(str(tz) for tz, visitors, bare, visitors_recent, bare_recent in rows
                  if tz and (bot_heavy(int(visitors_recent or 0), int(bare_recent or 0))
                             or bot_heavy(int(visitors or 0), int(bare or 0))))


def bot_zones(conn, now=None, max_age_hours: float = REFRESH_HOURS) -> list[str]:
    """The time zones that are bot-heavy now, from the runner's copy when it is younger than
    max_age_hours, else read once and kept. A failed read marks nothing (and is not kept): the
    numbers then include the automated visits, as they did before this rule, rather than stop a step."""
    st = cache.BOT_ZONES.get(conn)
    if st.get("zones") is not None and time.time() - float(st.get("at") or 0) < max_age_hours * 3600:
        return list(st["zones"])
    try:
        with conn.begin_nested():  # a failure rolls back to here, not the caller's whole transaction
            zones = zones_of(zone_rows(conn, now or db.utcnow()))
    except Exception as exc:  # noqa: BLE001 - a label must never cost a step
        log.warning("bot-heavy zones could not be read: %s", str(exc)[:160])
        return []
    cache.BOT_ZONES.put(conn, {"at": time.time(), "zones": zones})
    return zones


def tagged(since, zones, until=None):
    """The events from `since` (to `until`) as a subquery with two extra columns: `who`, the visitor,
    and `auto`, 1 on a likely automated view and 0 on everything else. Callers group it:

        t = readers.tagged(since, zones).c
        select(func.date(t.created_at), func.count()).where(t.type == "view", t.auto == 0).group_by(...)

    With no bot-heavy zone nothing can be automated, and the events are passed through as they are."""
    e = db.events.c
    who = _who(e)
    cols = [e.id, e.article_id, e.story_id, e.type, e.value, e.session, who.label("who"), e.source, e.tz, e.path,
            e.detail, e.created_at]
    if not zones:
        q = select(*cols, literal_column("0").label("auto")).where(e.created_at >= since)
        if until is not None:
            q = q.where(e.created_at < until)
        return q.subquery("ev")
    alone = func.count().over(partition_by=who) == 1
    # Plain 1 and 0, not bound values: the column is an integer on SQLite and Postgres alike.
    auto = case((and_(e.type == "view", alone, func.coalesce(e.source, "direct") == "direct", e.tz.in_(list(zones))),
                 literal_column("1")), else_=literal_column("0"))
    wide = select(*cols, auto.label("auto")).where(e.created_at >= since - EDGE)
    if until is not None:
        wide = wide.where(e.created_at < until + EDGE)
    wide = wide.subquery("ev_wide")
    q = select(wide).where(wide.c.created_at >= since)
    if until is not None:
        q = q.where(wide.c.created_at < until)
    return q.subquery("ev")
