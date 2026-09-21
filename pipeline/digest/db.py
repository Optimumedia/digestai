"""Database schema and session helpers. Works on SQLite (local) and Postgres (Supabase)."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import numpy as np
from sqlalchemy import (
    func,
    UniqueConstraint,
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

from . import config

log = logging.getLogger("digest.db")

metadata = MetaData()

sources = Table(
    "sources",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("key", String(120), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("url", Text, nullable=False),
    Column("kind", String(20), nullable=False, default="rss"),  # rss | hn | reddit
    Column("source_type", String(20), nullable=False, default="press"),  # primary | press | newsletter | community
    Column("category_hint", String(40)),
    Column("weight", Float, nullable=False, default=1.0),
    Column("fulltext", Boolean, nullable=False, default=True),
    Column("content_from_feed", Boolean, nullable=False, default=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("discovered", Boolean, nullable=False, default=False),
    Column("expires_at", DateTime(timezone=True)),
    Column("last_fetched_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("error_count", Integer, nullable=False, default=0),
    Column("engagement_ema", Float, nullable=False, default=0.0),
)

stories = Table(
    "stories",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", String(140), unique=True, nullable=False),
    Column("headline", Text, nullable=False),
    Column("summary_md", Text),
    Column("key_points", JSON),
    Column("why_it_matters", Text),
    Column("category", String(40)),
    Column("entities", JSON),
    Column("lead_article_id", Integer),
    Column("article_count", Integer, nullable=False, default=1),
    Column("importance", Integer, nullable=False, default=5),
    Column("score", Float, nullable=False, default=0.0),
    Column("embedding", JSON),
    Column("status", String(20), nullable=False, default="published"),  # published | unpublished | merged
    # A merged story (the same event as an older one, merge.py) keeps its row: its articles moved to the
    # story this points at, and the site redirects its old address there.
    Column("redirect_to", Integer),
    Column("pinned", Boolean, nullable=False, default=False),
    Column("thread_id", Integer),
    Column("pulse", Text),  # what practitioners are saying, from the HN thread
    # {"agree": sentence, "differ": [lines naming the outlet]} from the multi-source rewrite (upgrade.py)
    Column("source_notes", JSON),
    Column("pulse_at", DateTime(timezone=True)),
    Column("pushed_at", DateTime(timezone=True)),  # browser push alert sent
    Column("first_published_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # Stamped by the database on every insert and update (install_change_tracking), so a run reads
    # only the rows written since the previous one (cache.py). Never set by the pipeline itself.
    Column("rev", BigInteger),
)

threads = Table(
    "threads",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", String(140), unique=True, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("category", String(40)),
    Column("entities", JSON),
    Column("embedding", JSON),
    Column("story_count", Integer, nullable=False, default=1),
    Column("named_count", Integer, nullable=False, default=0),  # story_count when the model last named it
    Column("first_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False, default="published"),
    Column("rev", BigInteger),  # see stories.rev
)

articles = Table(
    "articles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("url", Text, unique=True, nullable=False),
    Column("source_id", Integer, ForeignKey("sources.id")),
    Column("story_id", Integer, ForeignKey("stories.id")),
    Column("slug", String(140), unique=True),
    Column("raw_title", Text),
    Column("title", Text),
    Column("headline", Text),
    Column("author", String(300)),
    Column("domain", String(200)),
    Column("published_at", DateTime(timezone=True)),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("lang", String(8)),
    Column("description", Text),
    Column("feed_content", Text),
    Column("content_md", Text),
    Column("content_text", Text),
    Column("word_count", Integer, nullable=False, default=0),
    Column("image_url", Text),
    Column("extraction_method", String(30)),
    Column("extraction_ok", Boolean, nullable=False, default=False),
    Column("show_fulltext", Boolean, nullable=False, default=True),
    Column("status", String(20), nullable=False, default="new"),
    # new -> extracted -> enriched -> published ; or rejected / unpublished
    Column("reject_reason", String(200)),
    Column("simhash", BigInteger),
    Column("summary_md", Text),
    Column("key_points", JSON),
    Column("why_it_matters", Text),
    Column("category", String(40)),
    Column("entities", JSON),
    Column("content_type", String(30)),
    Column("importance", Integer),
    Column("enrich_model", String(60)),
    Column("embedding", JSON),
    Column("predicted_score", Float),
    Column("engagement", Float, nullable=False, default=0.0),
    Column("model_release", JSON),  # {name, lab, kind, availability, license, context, link}
    Column("funding", JSON),  # {company, amount_usd, round, investors, valuation_usd}
    # AI at Work (/work): what a marketer or a small business can do with this, or NULL when there is
    # nothing to do. {fits, tool, maker, what_it_does, who_for, use_for, cost, effort, watch_out, link}
    Column("work_card", JSON),
    Column("discussion_site", String(20)),  # hn | reddit
    Column("discussion_url", Text),
    Column("discussion_points", Integer),
    Column("trend_score", Integer),  # web popularity outside HN: Mastodon shares, Reddit score
    Column("discussion_checked_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("rev", BigInteger),  # see stories.rev
)

newsletters = Table(
    "newsletters",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("date", String(10), unique=True, nullable=False),
    Column("subject", Text),
    Column("story_ids", JSON),
    Column("broadcast_id", String(40)),
    Column("public_url", Text),
    Column("sent_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("article_id", Integer),
    Column("story_id", Integer),
    Column("type", String(30), nullable=False),  # view | click_source | dwell | share | newsletter_click
    Column("value", Float, nullable=False, default=1.0),
    Column("session", String(64)),  # one browser tab
    Column("visitor", String(40)),  # random per browser, replaced every UTC day
    Column("source", String(60)),  # utm_source, else the referring host, else "direct"
    Column("tz", String(40)),  # the browser's time zone (e.g. Europe/Warsaw): gives the country, not the city
    Column("path", Text),
    # search: the query as typed (trimmed, lowercased, emails and long numbers removed);
    # depth: how far the reader got (top | summary | full_text | end). Empty for other types.
    Column("detail", String(100)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

topics = Table(
    "topics",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", String(140), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("kind", String(20)),  # companies | models | people
    Column("description", Text),  # two or three model-written sentences: what it is, why it matters now
    Column("story_count", Integer, nullable=False, default=0),
    Column("described_at_count", Integer, nullable=False, default=0),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

llm_usage = Table(
    "llm_usage",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("day", String(10), nullable=False),  # UTC date
    Column("provider", String(20), nullable=False),  # gemini | groq | ollama
    Column("requests", Integer, nullable=False, default=0),
    Column("exhausted", Boolean, nullable=False, default=False),
)

push_subscriptions = Table(
    "push_subscriptions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("endpoint", Text, nullable=False, unique=True),
    Column("p256dh", String(200), nullable=False),
    Column("auth", String(100), nullable=False),
    Column("topics", Text),  # reserved: JSON list of followed topics for targeted alerts
    Column("failures", Integer, nullable=False, default=0, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_ok_at", DateTime(timezone=True)),
)

social_posts = Table(
    "social_posts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("network", String(20), nullable=False),  # bluesky
    Column("kind", String(20), nullable=False),  # briefing | story
    Column("key", String(160), nullable=False),  # briefing date or story slug
    Column("uri", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("network", "kind", "key", name="social_posts_unique"),
)

# One row per UTC day, kept forever: the dashboard's period comparisons (day vs day ... year vs
# year). Reader events are pruned after 90 days, so these totals are the only long record.
# NULL means "not measured that day" (before tracking existed), which is not the same as zero.
daily_stats = Table(
    "daily_stats",
    metadata,
    Column("day", String(10), primary_key=True),  # YYYY-MM-DD, UTC
    Column("sessions", Integer),
    Column("visitors", Integer),
    Column("views", Integer),
    Column("dwell_seconds", Float),
    Column("dwell_reads", Integer),
    Column("clicks", Integer),
    Column("saves", Integer),
    Column("follows", Integer),
    Column("shares", Integer),
    Column("listens", Integer),
    Column("alert_signups", Integer),
    Column("searches", Integer),
    Column("stories_published", Integer),
    Column("articles_published", Integer),
    Column("articles_fetched", Integer),
    Column("social_posts", Integer),
    Column("crashed_steps", Integer),
    Column("google_clicks", Integer),
    Column("google_impressions", Integer),
    # Average position that day (impression-weighted, as Search Console reports it); NULL when
    # Google showed the site nowhere. position_sum = position x impressions, so a period's
    # average is sum(position_sum) / sum(impressions), not a mean of daily averages.
    Column("google_position", Float),
    Column("google_position_sum", Float),
    Column("google_queries", Integer),  # searches the site appeared for that day (Google hides rare ones)
    Column("db_read_kb", Float),  # estimated database reads by the pipeline that day (the free plan meters them)
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("step", String(30)),
    Column("stats", JSON),
)

# Stories, articles and threads removed from the database (by hand; the pipeline never deletes them), so the
# runner's copy drops them too. Filled by a database trigger; pruned after 30 days by tidy.py.
deleted_rows = Table(
    "deleted_rows",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("table_name", String(40), nullable=False),
    Column("row_id", Integer, nullable=False),
    Column("rev", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)

TRACKED_TABLES = ("stories", "articles", "threads")

_engine: Engine | None = None

# ---------------------------------------------------------------------------- read meter
# Supabase's free plan counts every byte the database sends (5 GB a month), so each step records
# roughly how much it read. The estimate is the row data plus the protocol's per-row and
# per-column framing; on Postgres the value lengths come from the result itself.
_bytes_read = [0]
PG_ROW_OVERHEAD, PG_COLUMN_OVERHEAD, PG_QUERY_OVERHEAD = 7, 4, 40


def bytes_read() -> int:
    """Estimated bytes read from the database by this process so far."""
    return _bytes_read[0]


def _value_size(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (str, bytes)):
        return len(v)
    if isinstance(v, bool):
        return 1
    if isinstance(v, int):
        return len(str(v))
    if isinstance(v, float):
        return 8
    return 26  # dates and times as text


def _count_rows(cursor, row):
    _bytes_read[0] += PG_ROW_OVERHEAD + sum(PG_COLUMN_OVERHEAD + _value_size(v) for v in row)
    return row


def pgresult_bytes(res) -> int:
    """Bytes of a Postgres result (psycopg's pgresult): the value lengths plus framing."""
    if res is None:
        return 0
    n, f = res.ntuples, res.nfields
    total = PG_QUERY_OVERHEAD + n * (PG_ROW_OVERHEAD + PG_COLUMN_OVERHEAD * f)
    # psycopg's binary PGresult has no get_length; get_value returns the raw bytes (None for NULL).
    length = getattr(res, "get_length", None) or (lambda r, c: len(res.get_value(r, c) or b""))
    for r in range(n):
        for c in range(f):
            total += length(r, c)
    return total


def install_read_meter(eng: Engine) -> None:
    if eng.dialect.name == "sqlite":
        # sqlite3 hands every fetched row to row_factory: count it there and return it unchanged.
        @event.listens_for(eng, "connect")
        def _meter_sqlite(dbapi_conn, _record):
            dbapi_conn.row_factory = _count_rows
    else:
        @event.listens_for(eng, "after_cursor_execute")
        def _meter_pg(conn, cursor, statement, parameters, context, executemany):
            try:
                _bytes_read[0] += pgresult_bytes(getattr(cursor, "pgresult", None))
            except Exception:  # noqa: BLE001 - measuring must never break a query
                pass


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = _normalize_url(config.DATABASE_URL)
        _engine = create_engine(url, future=True, pool_pre_ping=True)
        install_read_meter(_engine)
        # Checking every table's columns reads the database catalogue (tens of KB on Postgres); skip it
        # when this runner already brought this database up to this schema.
        schema_key = _schema_key(_engine)
        if _read_schema_marker() != schema_key:
            metadata.create_all(_engine)
            _migrate(_engine)
        if _engine.dialect.name == "postgresql":
            _harden_postgres(_engine)
        if _engine.dialect.name == "sqlite":
            with _engine.begin() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
        if install_change_tracking(_engine):
            _write_schema_marker(schema_key)
    return _engine


def _schema_key(eng: Engine) -> str:
    parts =[eng.url.render_as_string(hide_password=False), CHANGE_TRACKING_VERSION]
    for table in metadata.sorted_tables:
        parts += [f"{table.name}.{c.name}:{c.type!r}" for c in table.columns]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _read_schema_marker() -> str | None:
    try:
        return (config.CACHE_DIR / "schema.key").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_schema_marker(key: str) -> None:
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (config.CACHE_DIR / "schema.key").write_text(key, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------- change tracking
# Every insert and update of a story or an article gets a revision number from the database, and a
# deletion leaves a row in deleted_rows. A run keeps a copy of what it read (cache.py) and next time
# asks only for rows with a revision at or above the watermark it saw, so unchanged rows are never
# sent again. On Postgres the revision is the writing transaction's id and the watermark the oldest
# transaction still running, so a write that was in flight during the previous read is not missed.
# SQLite (one writer at a time) uses a counter table. tidy.py empties old text inside a transaction
# marked with revision_kept(), so storage clean-up does not make the rows look changed.

CHANGE_TRACKING_VERSION = "1"

CHANGE_TRACKING_PG_FUNCTION = """create or replace function public.digest_row_rev() returns trigger
  language plpgsql set search_path = public as $$
begin
  if tg_op = 'DELETE' then
    insert into deleted_rows (table_name, row_id, rev, created_at) values (tg_table_name, old.id, txid_current(), now());
    return old;
  end if;
  if coalesce(current_setting('digest.keep_rev', true), '') <> 'on' then
    new.rev := txid_current();
  end if;
  return new;
end $$;"""


def _sqlite_tracking_sql(table: str) -> list[str]:
    bump = "UPDATE digest_clock SET n = n + 1 WHERE id = 1;"
    stamp = f"UPDATE {table} SET rev = (SELECT n FROM digest_clock WHERE id = 1) WHERE id = NEW.id;"
    return [
        f"CREATE TRIGGER IF NOT EXISTS {table}_rev_insert AFTER INSERT ON {table} BEGIN {bump} {stamp} END",
        f"CREATE TRIGGER IF NOT EXISTS {table}_rev_update AFTER UPDATE ON {table} "
        f"WHEN NEW.rev IS OLD.rev AND (SELECT keep FROM digest_clock WHERE id = 1) = 0 BEGIN {bump} {stamp} END",
        f"CREATE TRIGGER IF NOT EXISTS {table}_rev_delete AFTER DELETE ON {table} BEGIN {bump} "
        f"INSERT INTO deleted_rows (table_name, row_id, rev, created_at) "
        f"VALUES ('{table}', OLD.id, (SELECT n FROM digest_clock WHERE id = 1), CURRENT_TIMESTAMP); END",
    ]


def _tracking_triggers(dialect: str) -> list[str]:
    if dialect == "sqlite":
        return [f"{t}_rev_{op}" for t in TRACKED_TABLES for op in ("insert", "update", "delete")]
    return [f"{t}_{kind}" for t in TRACKED_TABLES for kind in ("rev", "deleted")]


_tracking_ready: dict[str, bool] = {}


def install_change_tracking(eng: Engine) -> bool:
    """Create the revision triggers where missing. False when they could not be created (the cache
    then reads everything, as before)."""
    try:
        if eng.dialect.name == "sqlite":
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE IF NOT EXISTS digest_clock (id INTEGER PRIMARY KEY CHECK (id = 1), "
                                  "n INTEGER NOT NULL, keep INTEGER NOT NULL DEFAULT 0)"))
                conn.execute(text("INSERT OR IGNORE INTO digest_clock (id, n, keep) VALUES (1, 1, 0)"))
                for t in TRACKED_TABLES:
                    for stmt in _sqlite_tracking_sql(t):
                        conn.execute(text(stmt))
        elif eng.dialect.name == "postgresql":
            with eng.begin() as conn:
                conn.execute(text(CHANGE_TRACKING_PG_FUNCTION))  # replacing a function takes no table lock
            with eng.begin() as conn:
                have = {r[0] for r in conn.execute(text(
                    "select tgname from pg_trigger where not tgisinternal and tgname = any(:names)"),
                    {"names": _tracking_triggers("postgresql")}).all()}
                missing = [n for n in _tracking_triggers("postgresql") if n not in have]
                if missing:
                    # Creating a trigger locks the table briefly: give up fast and retry next run.
                    conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                    for t in TRACKED_TABLES:
                        if f"{t}_rev" in missing:
                            conn.execute(text(f"create trigger {t}_rev before insert or update on {t} "
                                              "for each row execute function public.digest_row_rev()"))
                        if f"{t}_deleted" in missing:
                            conn.execute(text(f"create trigger {t}_deleted after delete on {t} "
                                              "for each row execute function public.digest_row_rev()"))
        else:
            return False
    except Exception as exc:  # noqa: BLE001 - without tracking the cache falls back to full reads
        log.warning("could not install change tracking: %s", str(exc)[:160])
        _tracking_ready.pop(_engine_key(eng), None)
        return False
    _tracking_ready.pop(_engine_key(eng), None)
    return True


def _engine_key(eng: Engine) -> str:
    return eng.url.render_as_string(hide_password=False)


def change_tracking_ready(conn) -> bool:
    """Whether every revision trigger exists on this database (checked once per process)."""
    key = _engine_key(conn.engine)
    if key not in _tracking_ready:
        names = _tracking_triggers(conn.engine.dialect.name)
        try:
            if conn.engine.dialect.name == "sqlite":
                n = conn.execute(text("SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name IN ("
                                      + ",".join(f"'{x}'" for x in names) + ")")).scalar()
            elif conn.engine.dialect.name == "postgresql":
                n = conn.execute(text("select count(*) from pg_trigger where not tgisinternal and tgname = any(:names)"),
                                 {"names": names}).scalar()
            else:
                n = 0
        except Exception:  # noqa: BLE001
            n = 0
        _tracking_ready[key] = n == len(names)
    return _tracking_ready[key]


def watermark(conn) -> int:
    """Rows stamped with a revision at or above this value were written after this moment (or by a
    transaction still running now)."""
    if conn.engine.dialect.name == "sqlite":
        return int(conn.execute(text("SELECT n + 1 FROM digest_clock WHERE id = 1")).scalar())
    return int(conn.execute(text("select txid_snapshot_xmin(txid_current_snapshot())")).scalar())


class revision_kept:
    """Inside a transaction: updates do not get a new revision. Only for changes nothing reads
    through the cache (emptying old text and embeddings)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        if self.conn.engine.dialect.name == "sqlite":
            self.conn.execute(text("UPDATE digest_clock SET keep = 1 WHERE id = 1"))
        else:
            self.conn.execute(text("SET LOCAL digest.keep_rev = 'on'"))
        return self.conn

    def __exit__(self, *exc):
        if self.conn.engine.dialect.name == "sqlite":
            self.conn.execute(text("UPDATE digest_clock SET keep = 0 WHERE id = 1"))
        else:
            self.conn.execute(text("SET LOCAL digest.keep_rev = 'off'"))
        return False


# ---------------------------------------------------------------------------- embeddings
# Stored as "f16:" + base64 of little-endian float16 (1,030 bytes) instead of a JSON list of 384
# full-precision floats (~8,500 bytes). Rounding to float16 moves a cosine similarity between two
# unit vectors by well under 0.001 (tests/test_reads.py), far inside the clustering margins (0.82
# to merge, 0.03 lead margin, 0.78/0.88 for threads). Older rows keep their JSON lists; both read.
VEC_PREFIX = "f16:"


def pack_vec(values) -> str:
    arr = np.asarray(values, dtype=np.float32).astype("<f2")
    return VEC_PREFIX + base64.b64encode(arr.tobytes()).decode("ascii")


def unpack_vec(value):
    """A stored embedding (packed string, JSON list, or JSON text of a list) as float32, or None."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.startswith(VEC_PREFIX):
            return np.frombuffer(base64.b64decode(value[len(VEC_PREFIX):]), dtype="<f2").astype(np.float32)
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    return np.asarray(value, dtype=np.float32)


# ---------------------------------------------------------------------------- size
def database_size(conn) -> dict:
    """Database size in bytes and the biggest tables (Postgres: including indexes and TOAST)."""
    if conn.engine.dialect.name == "postgresql":
        total = conn.execute(text("select pg_database_size(current_database())")).scalar()
        tables = conn.execute(text(
            "select relname, pg_total_relation_size(relid) from pg_statio_user_tables "
            "where schemaname = 'public' order by 2 desc limit 6")).all()
        return {"bytes": int(total or 0), "tables": [{"name": n, "bytes": int(b or 0)} for n, b in tables]}
    if conn.engine.dialect.name == "sqlite":
        pages = conn.execute(text("PRAGMA page_count")).scalar() or 0
        size = conn.execute(text("PRAGMA page_size")).scalar() or 0
        tables = []
        try:  # dbstat exists only when SQLite was built with it
            tables = [{"name": n, "bytes": int(b)} for n, b in conn.execute(text(
                "SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 6")).all()]
        except Exception:  # noqa: BLE001
            pass
        return {"bytes": int(pages) * int(size), "tables": tables}
    return {"bytes": 0, "tables": []}


def _normalize_url(url: str) -> str:
    """Use the psycopg driver and percent-encode a raw password (spaces, &, @, # ...) so a
    connection string pasted straight from a dashboard works unchanged."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    m = re.match(r"^(postgresql)(\+\w+)?://([^:/@]+):(.*)@([^@]+)$", url, re.DOTALL)
    if m:
        scheme, driver, user, password, rest = m.groups()
        password = quote(unquote(password), safe="")
        return f"{scheme}+psycopg://{user}:{password}@{rest}"
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


EVENTS_GUARD_SQL = """create or replace function public.events_guard() returns trigger
  language plpgsql security definer set search_path = public as $$
begin
  -- Page views, listens and alert sign-ups happen on pages that are not stories: story_id may be
  -- empty, but a story_id that is given must exist.
  if new.story_id is not null and not exists (select 1 from stories s where s.id = new.story_id) then
    raise exception 'unknown story';
  end if;
  if new.created_at is null or new.created_at > now() + interval '5 minutes' or new.created_at < now() - interval '1 day' then
    new.created_at := now();
  end if;
  if length(coalesce(new.session, '')) > 40 or length(coalesce(new.path, '')) > 200
     or length(coalesce(new.visitor, '')) > 40 or length(coalesce(new.source, '')) > 60
     or length(coalesce(new.detail, '')) > 100 or length(coalesce(new.tz, '')) > 40 then
    raise exception 'payload too large';
  end if;
  -- A search query is kept only as a subject: no e-mail addresses or long numbers, even if typed.
  if new.detail is not null and new.type = 'search' then
    new.detail := left(regexp_replace(regexp_replace(lower(btrim(new.detail)), '[^[:space:]]+@[^[:space:]]+', '', 'g'), '[0-9]{5,}', '', 'g'), 100);
  end if;
  if (select count(*) from events e where e.session = new.session and e.created_at > now() - interval '1 minute') >= 30 then
    raise exception 'too many events';
  end if;
  return new;
end $$;"""


# What the public key may insert into events (also in supabase/schema.sql). A type that is not in
# this list is refused by the database, so a new reader event needs its name added here.
# AI at Work (/work): what readers do with a card. try = the card's "Try it" link, copy_prompt = the
# starter prompt copied, expand = "How to set it up" opened, next_click = the page's next-step link,
# card_view = a card at least half on screen (once per card per page view, the site sends at most 10).
# They carry the tool's name (or the link's label) in detail and, on /work pages, no story_id: the
# page is the path, which every event already has (the guard lets story_id be empty).
WORK_EVENT_TYPES = ("try", "copy_prompt", "expand", "next_click", "card_view")
PUBLIC_EVENT_TYPES = ("view", "click_source", "dwell", "share", "newsletter_click", "save", "follow", "comment",
                      "push_on", "listen", "search", "depth", *WORK_EVENT_TYPES)
# The event types that may carry a detail (anything else must send none).
DETAIL_EVENT_TYPES = ("search", "depth", *WORK_EVENT_TYPES)
DEPTH_STAGES = ("top", "summary", "full_text", "end")
_quoted = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
EVENTS_POLICY_SQL = [
    'DROP POLICY IF EXISTS "public can log events" ON events',
    f"""CREATE POLICY "public can log events" ON events
  FOR INSERT TO anon
  WITH CHECK (
    type IN ({_quoted(PUBLIC_EVENT_TYPES)})
    AND value >= 0 AND value <= 3600
    AND (type <> 'depth' OR (value <= 100 AND detail IN ({_quoted(DEPTH_STAGES)})))
    AND (detail IS NULL OR type IN ({_quoted(DETAIL_EVENT_TYPES)}))
  )""",
]


def _harden_postgres(eng: Engine) -> None:
    """On Supabase every table is reachable through the public REST API with the publishable
    key unless row security is on and grants are revoked. Do that for every pipeline table on
    every start, so a table added in a later version is never exposed. Only `events` keeps an
    insert grant (the site's reader beacon); supabase/schema.sql adds its policy and guard."""
    try:
        with eng.begin() as conn:
            # ALTER TABLE takes an exclusive lock; give up quickly rather than queue behind a
            # running pipeline (a local run and the CI run once deadlocked each other here).
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            for table in metadata.sorted_tables:
                conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))
                conn.execute(text(f'REVOKE ALL ON "{table.name}" FROM anon, authenticated'))
            conn.execute(text('GRANT INSERT ON events TO anon'))
            conn.execute(text('GRANT USAGE, SELECT ON SEQUENCE events_id_seq TO anon'))
            conn.execute(text('GRANT INSERT ON push_subscriptions TO anon'))
            conn.execute(text('GRANT USAGE, SELECT ON SEQUENCE push_subscriptions_id_seq TO anon'))
    except Exception as exc:  # noqa: BLE001 - roles may not exist outside Supabase
        log.warning("could not harden tables: %s", str(exc)[:120])
    # The insert guard on events (also in supabase/schema.sql). Kept here so a change reaches the
    # database on the next run; replacing a function takes no table lock.
    try:
        with eng.begin() as conn:
            conn.execute(text(EVENTS_GUARD_SQL))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not update the events guard: %s", str(exc)[:120])
    # The insert policy: which event types the site may send. Replacing a policy locks the table
    # briefly, so give up fast and try again next run rather than wait behind a busy pipeline.
    try:
        with eng.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            for stmt in EVENTS_POLICY_SQL:
                conn.execute(text(stmt))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not update the events policy: %s", str(exc)[:120])


def _migrate(eng: Engine) -> None:
    """Add columns that exist in the models but not yet in a database created by an older version.

    create_all only creates missing tables; a persisted database needs the new columns added
    in place. ADD COLUMN works on both SQLite and Postgres.
    """
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=eng.dialect)}'
                if col.default is not None and getattr(col.default, "arg", None) is not None and not callable(col.default.arg):
                    val = col.default.arg
                    ddl += f" DEFAULT {int(val) if isinstance(val, bool) else repr(val)}"
                conn.execute(text(ddl))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive datetimes as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_z(value: datetime | None) -> str | None:
    """A timestamp as the site's JSON carries it: UTC, ISO 8601, with a Z. None stays None."""
    value = as_utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def to_signed64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed64(value: int) -> int:
    return value + (1 << 64) if value < 0 else value
