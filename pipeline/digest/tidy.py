"""Step: keep the database small. The free plan allows 500 MB, and most of it was text nothing reads.

Each run empties, in bounded batches, columns no step needs any more (rows themselves stay, so
duplicate checks, the dashboard's counts and links keep working):

- content_text and feed_content once an article is summarised or rejected: only the gate and the
  summariser read them (the site shows content_md);
- content_md and embedding of rejected articles: nothing shows or compares them;
- article embeddings older than 35 days (ranking learns from the last 30; clustering looks back
  72 hours), story embeddings older than 60 days and those of threads idle for 60 days (threads
  look back 14 days);
- article text older than the export window plus 10 days (no page shows it any more).

The updates run inside db.revision_kept, so they do not make rows look changed to the runner's
copy (cache.py), and with a short lock timeout, so they never hold up a pipeline step for long.

Postgres keeps the freed space inside its files for new rows rather than shrinking them, so the
database stops growing instead of getting smaller. A daily VACUUM (ANALYZE) of the articles table
makes the space reusable promptly (autovacuum would get there too). VACUUM FULL would give the space
back to the plan but locks the table while it rewrites it; run it by hand once if ever needed.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, null, or_, select, text, update

from . import config, db

log = logging.getLogger("digest.tidy")

MAX_ROWS_PER_RUN = 2000
VACUUM_EVERY_HOURS = 24
VACUUM_MIN_DEAD_ROWS = 2000
DELETED_ROWS_DAYS = 30


def rules(now) -> list[tuple[str, object, object, dict]]:
    """(name, table, which rows, what to empty)."""
    a, s, t = db.articles.c, db.stories.c, db.threads.c
    old_text = now - timedelta(days=config.EXPORT_DAYS + 10)
    return [
        ("summarised_text", db.articles,
         and_(a.status.in_(["enriched", "published", "unpublished", "rejected"]), or_(a.content_text.isnot(None), a.feed_content.isnot(None))),
         {"content_text": null(), "feed_content": null()}),
        ("rejected_text", db.articles,
         and_(a.status == "rejected", or_(a.content_md.isnot(None), a.embedding.isnot(None))),
         {"content_md": null(), "embedding": null()}),
        ("old_article_embeddings", db.articles,
         and_(a.created_at < now - timedelta(days=35), a.embedding.isnot(None)), {"embedding": null()}),
        ("old_story_embeddings", db.stories,
         and_(s.first_published_at < now - timedelta(days=60), s.embedding.isnot(None)), {"embedding": null()}),
        ("old_thread_embeddings", db.threads,
         and_(t.updated_at < now - timedelta(days=60), t.embedding.isnot(None)), {"embedding": null()}),
        ("old_article_text", db.articles,
         and_(a.created_at < old_text, or_(a.content_md.isnot(None), a.description.isnot(None))),
         {"content_md": null(), "description": null()}),
    ]


def clean(eng, now=None, limit: int = MAX_ROWS_PER_RUN) -> dict[str, int]:
    """Empty at most `limit` rows' worth of unused columns in total; returns rows changed per rule."""
    now = now or db.utcnow()
    done: dict[str, int] = {}
    budget = limit
    with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            conn.execute(text("SET LOCAL statement_timeout = '60s'"))
        with db.revision_kept(conn):
            for name, table, where, values in rules(now):
                if budget <= 0:
                    break
                # The ids are chosen inside the database: nothing is read back but the count.
                ids = select(table.c.id).where(where).limit(budget)
                n = conn.execute(update(table).where(table.c.id.in_(ids)).values(**values)).rowcount or 0
                if n:
                    done[name] = n
                    budget -= n
        conn.execute(db.deleted_rows.delete().where(db.deleted_rows.c.created_at < now - timedelta(days=DELETED_ROWS_DAYS)))
    return done


def vacuum(eng) -> str | None:
    """Postgres only, at most daily and only after enough rows changed: VACUUM (ANALYZE) articles.
    Returns what happened, or None when nothing was due."""
    if eng.dialect.name != "postgresql":
        return None
    with eng.connect() as conn:
        row = conn.execute(text(
            "select n_dead_tup, extract(epoch from now() - greatest(last_vacuum, last_autovacuum)) / 3600 "
            "from pg_stat_user_tables where relname = 'articles'")).first()
    if not row:
        return None
    dead, hours = int(row[0] or 0), row[1]
    if dead < VACUUM_MIN_DEAD_ROWS or (hours is not None and float(hours) < VACUUM_EVERY_HOURS):
        return None
    # VACUUM cannot run inside a transaction; it does not block reads or writes.
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SET statement_timeout = '120s'"))
        try:
            conn.execute(text("VACUUM (ANALYZE) articles"))
        finally:
            conn.execute(text("RESET statement_timeout"))
    return f"vacuumed articles ({dead} dead rows)"


def run() -> dict:
    eng = db.engine()
    stats: dict = {"cleaned": clean(eng)}
    try:
        done = vacuum(eng)
        if done:
            stats["vacuum"] = done
    except Exception as exc:  # noqa: BLE001 - a skipped vacuum costs nothing; autovacuum still runs
        log.warning("vacuum skipped: %s", str(exc)[:160])
        stats["vacuum"] = "skipped"
    return stats
