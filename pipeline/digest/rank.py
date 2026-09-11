"""Step 6: learn from reader behaviour.

1. Engagement per article from the events table (views, clicks to source, dwell, shares).
2. A ridge regression from article embeddings to engagement predicts how well new articles
   will do before anyone has read them.
3. Story score = predicted engagement + editorial importance + recency; drives ordering.
4. Discovery: topics that perform well become temporary search-feed sources, and source
   weights drift toward the sources whose articles readers actually open.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from urllib.parse import quote_plus

import numpy as np
from sqlalchemy import func, insert, select, update

from . import config, db
from .textutil import keywords

log = logging.getLogger("digest.rank")

EVENT_WEIGHTS = {"view": 1.0, "click_source": 3.0, "dwell": 1.0 / 30.0, "share": 5.0, "newsletter_click": 2.0}
MIN_TRAINING_ARTICLES = 40
RIDGE_LAMBDA = 1.0
HALF_LIFE_HOURS = 18.0
MAX_DISCOVERED_SOURCES = 8
DISCOVERY_TTL_DAYS = 3


def _engagement(conn) -> dict[int, float]:
    since = db.utcnow() - timedelta(days=30)
    rows = conn.execute(
        select(db.events.c.article_id, db.events.c.type, func.sum(db.events.c.value))
        .where(db.events.c.created_at >= since, db.events.c.article_id.isnot(None))
        .group_by(db.events.c.article_id, db.events.c.type)
    ).all()
    raw: dict[int, float] = {}
    for article_id, etype, total in rows:
        w = EVENT_WEIGHTS.get(etype, 0.0)
        val = float(total or 0)
        if etype == "dwell":
            val = min(val, 600.0)
        raw[article_id] = raw.get(article_id, 0.0) + w * val
    return raw


def _recency(published_at, now) -> float:
    if published_at is None:
        return 0.5
    hours = max(0.0, (now - db.as_utc(published_at)).total_seconds() / 3600)
    return math.pow(0.5, hours / HALF_LIFE_HOURS)


def train_and_predict(conn) -> dict:
    stats = {"trained": False, "training_rows": 0}
    raw = _engagement(conn)
    now = db.utcnow()
    since = now - timedelta(days=30)
    arts = conn.execute(
        select(db.articles.c.id, db.articles.c.embedding, db.articles.c.published_at, db.articles.c.importance)
        .where(db.articles.c.status == "published", db.articles.c.created_at >= since, db.articles.c.embedding.isnot(None))
    ).all()
    if not arts:
        return stats

    # Engagement rate: events per day since publication, log-scaled.
    y_by_id: dict[int, float] = {}
    for a in arts:
        e = raw.get(a.id, 0.0)
        age_days = max(0.25, (now - (db.as_utc(a.published_at) or now)).total_seconds() / 86400)
        y_by_id[a.id] = math.log1p(e / age_days)
        conn.execute(update(db.articles).where(db.articles.c.id == a.id).values(engagement=e))

    trained = [a for a in arts if raw.get(a.id, 0.0) > 0]
    weights = None
    if len(trained) >= MIN_TRAINING_ARTICLES:
        X = np.asarray([a.embedding for a in trained], dtype=np.float64)
        y = np.asarray([y_by_id[a.id] for a in trained], dtype=np.float64)
        y_mean = y.mean()
        XtX = X.T @ X + RIDGE_LAMBDA * np.eye(X.shape[1])
        weights = np.linalg.solve(XtX, X.T @ (y - y_mean))
        y_max = max(float(y.max()), 1e-6)
        stats.update(trained=True, training_rows=len(trained))

    for a in arts:
        if weights is not None:
            pred = float(np.asarray(a.embedding, dtype=np.float64) @ weights) + y_mean
            pred = max(0.0, min(1.0, pred / y_max))
        else:
            pred = (a.importance or 5) / 10.0
        conn.execute(update(db.articles).where(db.articles.c.id == a.id).values(predicted_score=pred))
    return stats


def score_stories(conn) -> int:
    now = db.utcnow()
    since = now - timedelta(days=config.EXPORT_DAYS)
    stories_rows = conn.execute(select(db.stories).where(db.stories.c.updated_at >= since)).all()
    n = 0
    for s in stories_rows:
        members = conn.execute(
            select(db.articles.c.predicted_score, db.articles.c.engagement, db.articles.c.published_at)
            .where(db.articles.c.story_id == s.id, db.articles.c.status == "published")
        ).all()
        if not members:
            continue
        predicted = max((m.predicted_score or 0.0) for m in members)
        engagement = sum((m.engagement or 0.0) for m in members)
        latest = max((db.as_utc(m.published_at) or now) for m in members)
        breadth = min(1.0, math.log1p(len(members)) / math.log(6))
        score = (
            0.35 * predicted
            + 0.25 * (s.importance or 5) / 10.0
            + 0.25 * _recency(latest, now)
            + 0.10 * breadth
            + 0.05 * min(1.0, math.log1p(engagement) / 6.0)
        )
        conn.execute(update(db.stories).where(db.stories.c.id == s.id).values(score=round(score, 4)))
        n += 1
    return n


def update_source_weights(conn) -> None:
    """Exponential moving average of engagement per source, nudging curated weights ±30%."""
    since = db.utcnow() - timedelta(days=14)
    rows = conn.execute(
        select(db.articles.c.source_id, func.avg(db.articles.c.engagement))
        .where(db.articles.c.status == "published", db.articles.c.created_at >= since)
        .group_by(db.articles.c.source_id)
    ).all()
    if not rows:
        return
    values = [float(v or 0.0) for _, v in rows]
    mean = sum(values) / len(values)
    if mean <= 0:
        return
    for source_id, avg in rows:
        rel = min(2.0, float(avg or 0.0) / mean)  # 1.0 = average source
        conn.execute(update(db.sources).where(db.sources.c.id == source_id)
                     .values(engagement_ema=db.sources.c.engagement_ema * 0.7 + rel * 0.3))


def discover(conn) -> int:
    """Turn the best-performing topics into temporary Bing News search feeds."""
    since = db.utcnow() - timedelta(days=7)
    top = conn.execute(
        select(db.articles.c.headline, db.articles.c.entities, db.articles.c.engagement)
        .where(db.articles.c.status == "published", db.articles.c.created_at >= since, db.articles.c.engagement > 0)
        .order_by(db.articles.c.engagement.desc()).limit(25)
    ).all()
    if len(top) < 5:
        return 0
    counts: dict[str, float] = {}
    for a in top:
        ents = a.entities or {}
        names = (ents.get("companies") or []) + (ents.get("models") or [])
        terms = names or keywords(a.headline or "", 2)
        for t in terms:
            t = t.strip()
            if 2 < len(t) < 40:
                counts[t] = counts.get(t, 0.0) + math.log1p(a.engagement or 0.0)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:MAX_DISCOVERED_SOURCES]
    created = 0
    for term, _ in ranked:
        key = f"discover-{term.lower().replace(' ', '-')[:50]}"
        url = f"https://www.bing.com/news/search?q={quote_plus(term + ' AI')}&format=rss"
        expires = db.utcnow() + timedelta(days=DISCOVERY_TTL_DAYS)
        existing = conn.execute(select(db.sources.c.id).where(db.sources.c.key == key)).first()
        if existing:
            conn.execute(update(db.sources).where(db.sources.c.id == existing.id)
                         .values(enabled=True, expires_at=expires, url=url))
        else:
            conn.execute(insert(db.sources).values(
                key=key, name=f"Search: {term}", url=url, kind="rss", category_hint=None, weight=0.7,
                fulltext=True, content_from_feed=False, enabled=True, discovered=True, expires_at=expires,
            ))
            created += 1
    return created


def run() -> dict:
    eng = db.engine()
    with eng.begin() as conn:
        stats = train_and_predict(conn)
        stats["stories_scored"] = score_stories(conn)
        update_source_weights(conn)
        stats["discovered_sources"] = discover(conn)
    return stats
