"""Database schema and session helpers. Works on SQLite (local) and Postgres (Supabase)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
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
    text,
)
from sqlalchemy.engine import Engine

from . import config

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
    Column("status", String(20), nullable=False, default="published"),  # published | unpublished
    Column("pinned", Boolean, nullable=False, default=False),
    Column("first_published_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
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
    Column("discussion_site", String(20)),  # hn | reddit
    Column("discussion_url", Text),
    Column("discussion_points", Integer),
    Column("discussion_checked_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
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
    Column("session", String(64)),
    Column("path", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
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

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        _engine = create_engine(url, future=True, pool_pre_ping=True)
        metadata.create_all(_engine)
        if _engine.dialect.name == "sqlite":
            with _engine.begin() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
    return _engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive datetimes as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_signed64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed64(value: int) -> int:
    return value + (1 << 64) if value < 0 else value
