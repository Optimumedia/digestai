"""Step: rank like a feed, from two kinds of signal.

External popularity: what the web is already reacting to (Hacker News points, Reddit score,
Mastodon shares, how many outlets cover the story, whether the primary source is in it).
Internal engagement: what our readers do (views, time on page, clicks to the source, saves,
follows, shares), recorded in the events table when Supabase is configured.

1. Engagement per article from events (30 days), decayed into a rate.
2. A ridge regression from [embedding, popularity, breadth, primary, importance] to that
   rate predicts how new stories will do before anyone has read them. Until there is enough
   reader data, external popularity stands in as the target, so the model still learns
   what kind of story the web reacts to.
3. Story score = predicted engagement + editorial importance + freshness + breadth +
   popularity velocity, which drives the front page and the briefing.
4. Feedback into sourcing: source weights drift toward the sources whose stories perform,
   and hot entities become temporary search feeds.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from urllib.parse import quote_plus

import numpy as np
from sqlalchemy import bindparam, func, insert, select, update

from . import config, db
from .textutil import keywords

log = logging.getLogger("digest.rank")

EVENT_WEIGHTS = {"view": 1.0, "click_source": 3.0, "dwell": 1.0 / 30.0, "share": 5.0, "save": 4.0,
                 "follow": 2.0, "newsletter_click": 2.0, "comment": 4.0}
MIN_TRAINING_ARTICLES = 40
RIDGE_LAMBDA = 1.0
HALF_LIFE_HOURS = 18.0
MAX_DISCOVERED_SOURCES = 8
DISCOVERY_TTL_DAYS = 3


def _engagement(conn) -> dict[int, float]:
    """Engagement per article, counting each session at most once per event type per day,
    so a single visitor (or a script) cannot inflate a story by reloading it."""
    since = db.utcnow() - timedelta(days=30)
    # Prune old events first: nothing past 90 days is used anywhere.
    conn.execute(db.events.delete().where(db.events.c.created_at < db.utcnow() - timedelta(days=90)))
    day = func.date(db.events.c.created_at)
    # One row per article, type, session and day. Dwell adds up across the visits of a session
    # (the browser sends one increment per visible stretch); other types count once.
    rows = conn.execute(
        select(db.events.c.article_id, db.events.c.type, db.events.c.session, day.label("d"),
               func.sum(db.events.c.value).label("total"), func.max(db.events.c.value).label("mx"))
        .where(db.events.c.created_at >= since, db.events.c.article_id.isnot(None))
        .group_by(db.events.c.article_id, db.events.c.type, db.events.c.session, day)
    ).all()
    raw: dict[int, float] = {}
    for article_id, etype, _session, _d, total, mx in rows:
        w = EVENT_WEIGHTS.get(etype, 0.0)
        # Ten minutes of reading is the most one visit may count for; other events count once.
        val = min(float(total or 0), 600.0) if etype == "dwell" else min(float(mx or 0), 1.0)
        raw[article_id] = raw.get(article_id, 0.0) + w * val
    return raw


def popularity(points: int | None, trend: int | None) -> float:
    """External popularity on a log scale: 0 for nothing, ~5 for a big HN thread."""
    return math.log1p(max(points or 0, 0)) + 0.6 * math.log1p(max(trend or 0, 0))


def _recency(published_at, now) -> float:
    if published_at is None:
        return 0.5
    hours = max(0.0, (now - db.as_utc(published_at)).total_seconds() / 3600)
    return math.pow(0.5, hours / HALF_LIFE_HOURS)


def _features(a, breadth: int, has_primary: bool) -> list[float]:
    return [popularity(a.discussion_points, a.trend_score) / 6.0, math.log1p(breadth) / 2.0,
            1.0 if has_primary else 0.0, (a.importance or 5) / 10.0]


def train_and_predict(conn) -> dict:
    stats = {"trained": False, "training_rows": 0, "target": None}
    raw = _engagement(conn)
    now = db.utcnow()
    since = now - timedelta(days=30)
    arts = conn.execute(
        select(db.articles.c.id, db.articles.c.embedding, db.articles.c.published_at, db.articles.c.importance,
               db.articles.c.discussion_points, db.articles.c.trend_score, db.articles.c.story_id, db.articles.c.domain,
               db.articles.c.engagement, db.articles.c.predicted_score)
        .where(db.articles.c.status == "published", db.articles.c.created_at >= since, db.articles.c.embedding.isnot(None))
    ).all()
    if not arts:
        return stats
    story_size: dict[int, int] = {}
    story_primary: dict[int, bool] = {}
    from .export import PRIMARY_DOMAINS

    for a in arts:
        story_size[a.story_id] = story_size.get(a.story_id, 0) + 1
        if a.domain in PRIMARY_DOMAINS:
            story_primary[a.story_id] = True

    # Target: reader engagement rate when we have it, otherwise external popularity.
    engaged = [a for a in arts if raw.get(a.id, 0.0) > 0]
    use_engagement = len(engaged) >= MIN_TRAINING_ARTICLES
    stats["target"] = "engagement" if use_engagement else "web popularity"
    y_by_id: dict[int, float] = {}
    eng_rows: list[dict] = []
    for a in arts:
        if use_engagement:
            age_days = max(0.25, (now - (db.as_utc(a.published_at) or now)).total_seconds() / 86400)
            y_by_id[a.id] = math.log1p(raw.get(a.id, 0.0) / age_days)
        else:
            y_by_id[a.id] = popularity(a.discussion_points, a.trend_score)
        if abs((a.engagement or 0.0) - raw.get(a.id, 0.0)) > 1e-9:
            eng_rows.append({"aid": a.id, "eng": raw.get(a.id, 0.0)})
    # One batched write instead of a round trip per article (this loop was most of the step's time).
    if eng_rows:
        conn.execute(update(db.articles).where(db.articles.c.id == bindparam("aid")).values(engagement=bindparam("eng")), eng_rows)

    train = engaged if use_engagement else [a for a in arts if y_by_id[a.id] > 0]
    weights = None
    if len(train) >= MIN_TRAINING_ARTICLES // 2:
        X = np.asarray([list(a.embedding) + _features(a, story_size[a.story_id], story_primary.get(a.story_id, False)) for a in train], dtype=np.float64)
        y = np.asarray([y_by_id[a.id] for a in train], dtype=np.float64)
        y_mean = y.mean()
        XtX = X.T @ X + RIDGE_LAMBDA * np.eye(X.shape[1])
        weights = np.linalg.solve(XtX, X.T @ (y - y_mean))
        y_max = max(float(y.max()), 1e-6)
        stats.update(trained=True, training_rows=len(train))

    pred_rows: list[dict] = []
    for a in arts:
        if weights is not None:
            x = np.asarray(list(a.embedding) + _features(a, story_size[a.story_id], story_primary.get(a.story_id, False)), dtype=np.float64)
            pred = float(x @ weights) + y_mean
            pred = max(0.0, min(1.0, pred / y_max))
        else:
            pred = min(1.0, 0.5 * (a.importance or 5) / 10.0 + 0.5 * popularity(a.discussion_points, a.trend_score) / 6.0)
        if a.predicted_score is None or abs(a.predicted_score - pred) > 1e-4:
            pred_rows.append({"aid": a.id, "pred": pred})
    if pred_rows:
        conn.execute(update(db.articles).where(db.articles.c.id == bindparam("aid")).values(predicted_score=bindparam("pred")), pred_rows)
    stats["predictions_changed"] = len(pred_rows)
    return stats


def score_stories(conn) -> int:
    now = db.utcnow()
    since = now - timedelta(days=config.EXPORT_DAYS)
    stories_rows = conn.execute(
        select(db.stories.c.id, db.stories.c.importance, db.stories.c.first_published_at, db.stories.c.score)
        .where(db.stories.c.updated_at >= since)
    ).all()
    # One query for every member article instead of one query per story.
    members_by_story: dict[int, list] = {}
    for m in conn.execute(
        select(db.articles.c.story_id, db.articles.c.predicted_score, db.articles.c.engagement, db.articles.c.published_at,
               db.articles.c.discussion_points, db.articles.c.trend_score, db.articles.c.content_type,
               db.sources.c.source_type)
        .join(db.sources, db.articles.c.source_id == db.sources.c.id, isouter=True)
        .join(db.stories, db.articles.c.story_id == db.stories.c.id)
        .where(db.stories.c.updated_at >= since, db.articles.c.status == "published")
    ).all():
        members_by_story.setdefault(m.story_id, []).append(m)

    updates: list[dict] = []
    n = 0
    for s in stories_rows:
        members = members_by_story.get(s.id)
        if not members:
            continue
        n += 1
        predicted = max((m.predicted_score or 0.0) for m in members)
        engagement = sum((m.engagement or 0.0) for m in members)
        latest = max((db.as_utc(m.published_at) or now) for m in members)
        first = min(db.as_utc(s.first_published_at) or latest, latest)
        # Breaking news earns its freshness; a daily digest or a tutorial published this morning
        # is not "new" in the same sense, so its recency counts for less.
        breaking = any((m.content_type in (None, "news", "product", "research")) and m.source_type != "newsletter" for m in members)
        recency_weight = 1.0 if breaking else 0.55
        breadth = min(1.0, math.log1p(len(members)) / math.log(6))
        pop = max(popularity(m.discussion_points, m.trend_score) for m in members)
        age_h = max(1.0, (now - first).total_seconds() / 3600)
        velocity = min(1.0, (pop / 6.0) * (24.0 / max(age_h, 6.0)))  # popularity gained fast counts more
        # Freshness belongs to when the story broke. A new article on a days-old story is a
        # development, worth at most half the freshness of genuinely new news.
        freshness = max(_recency(first, now), 0.5 * _recency(latest, now))
        score = round(
            0.30 * predicted
            + 0.20 * (s.importance or 5) / 10.0
            + 0.22 * freshness * recency_weight
            + 0.10 * breadth
            + 0.10 * velocity
            + 0.08 * min(1.0, math.log1p(engagement) / 6.0),
            4,
        )
        if s.score is None or abs(s.score - score) > 1e-4:
            updates.append({"sid": s.id, "sc": score})
    if updates:
        conn.execute(update(db.stories).where(db.stories.c.id == bindparam("sid")).values(score=bindparam("sc")), updates)
    return n


def update_source_weights(conn) -> None:
    """Sources whose articles perform (readers or the web) drift up; the rest drift down."""
    since = db.utcnow() - timedelta(days=14)
    rows = conn.execute(
        select(db.articles.c.source_id, db.articles.c.engagement, db.articles.c.discussion_points, db.articles.c.trend_score)
        .where(db.articles.c.status == "published", db.articles.c.created_at >= since)
    ).all()
    if not rows:
        return
    per_source: dict[int, list[float]] = {}
    for sid, eng, pts, trend in rows:
        per_source.setdefault(sid, []).append(float(eng or 0.0) + popularity(pts, trend))
    means = {sid: sum(v) / len(v) for sid, v in per_source.items()}
    overall = sum(means.values()) / len(means)
    if overall <= 0:
        return
    for sid, avg in means.items():
        rel = min(2.0, avg / overall)
        conn.execute(update(db.sources).where(db.sources.c.id == sid)
                     .values(engagement_ema=db.sources.c.engagement_ema * 0.7 + rel * 0.3))


def discover(conn) -> int:
    """Turn the best-performing topics into temporary Bing News search feeds."""
    since = db.utcnow() - timedelta(days=7)
    rows = conn.execute(
        select(db.articles.c.headline, db.articles.c.entities, db.articles.c.engagement,
               db.articles.c.discussion_points, db.articles.c.trend_score)
        .where(db.articles.c.status == "published", db.articles.c.created_at >= since)
    ).all()
    scored = [(float(a.engagement or 0.0) + 3.0 * popularity(a.discussion_points, a.trend_score), a) for a in rows]
    top = [a for s, a in sorted(scored, key=lambda x: -x[0]) if s > 0][:25]
    if len(top) < 5:
        return 0
    counts: dict[str, float] = {}
    for a in top:
        ents = a.entities or {}
        names = (ents.get("companies") or []) + (ents.get("models") or [])
        terms = names or keywords(a.headline or "", 2)
        weight = math.log1p(float(a.engagement or 0.0) + 3.0 * popularity(a.discussion_points, a.trend_score))
        for t in terms:
            t = t.strip()
            if 2 < len(t) < 40:
                counts[t] = counts.get(t, 0.0) + weight
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
                key=key, name=f"Search: {term}", url=url, kind="rss", source_type="press", category_hint=None, weight=0.7,
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
