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
   popularity velocity + community traction (a bounded bonus for a big HN thread), which
   drives the front page and the briefing.
4. Feedback into sourcing: source weights drift toward the sources whose stories perform,
   and hot entities become temporary search feeds.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import numpy as np
from sqlalchemy import and_, bindparam, case, func, insert, or_, select, update

from . import cache, config, db

log = logging.getLogger("digest.rank")

EVENT_WEIGHTS = {"view": 1.0, "click_source": 3.0, "dwell": 1.0 / 30.0, "share": 5.0, "save": 4.0,
                 "follow": 2.0, "newsletter_click": 2.0, "comment": 4.0}
MIN_TRAINING_ARTICLES = 40
RIDGE_LAMBDA = 1.0
HALF_LIFE_HOURS = 18.0
ENGAGEMENT_FULL_HOURS = 24  # reader engagement is recounted in full this often (_engagement)
LATE_EVENT_HOURS = 25  # an event may carry a time up to a day before it was recorded
MAX_DISCOVERED_SOURCES = config.MAX_DISCOVERED_SOURCES
DISCOVERY_TTL_DAYS = 3
# Entity names that are not a company or model worth a search feed: publications, funds,
# institutions, events, laws ("Scaleup Europe Fund", "The Information", "EU AI Act").
GENERIC_TERM = re.compile(
    r"^(the|a|an)\s|\b(fund|funds|capital|ventures?|partners|holdings|group|bank|university|institute|"
    r"commission|council|government|ministry|department|agency|association|foundation|news|times|journal|"
    r"post|media|information|report|index|initiative|programme|program|project|act|summit|conference|week|"
    r"court|parliament|congress|senate|white house|pentagon)\b",
    re.IGNORECASE,
)
_DASHES = re.compile(r"[‐-―−]")


def _engagement(conn) -> dict[int, float]:
    """Engagement per article, counting each session at most once per event type per day,
    so a single visitor (or a script) cannot inflate a story by reloading it."""
    since = db.utcnow() - timedelta(days=30)
    # Prune old events first: nothing past 90 days is used anywhere.
    conn.execute(db.events.delete().where(db.events.c.created_at < db.utcnow() - timedelta(days=90)))
    # Views of the previous site's /article/ addresses were recorded from our not-found page before it
    # stopped counting (15 Sep); they are crawlers re-checking old links, not readers.
    conn.execute(db.events.delete().where(db.events.c.path.like("/article/%")))
    e = db.events.c
    day = func.date(e.created_at)

    def count(*where) -> dict[int, float]:
        # One row per article, type, session and day. Dwell adds up across the visits of a session
        # (the browser sends one increment per visible stretch); other types count once. The database
        # adds these up per article and type, so this reads one row per article with readers, not one
        # per visit.
        visits = (
            select(e.article_id, e.type, func.sum(e.value).label("total"), func.max(e.value).label("mx"))
            .where(e.created_at >= since, e.article_id.isnot(None), *where)
            .group_by(e.article_id, e.type, e.session, day)
        ).subquery()
        # Ten minutes of reading is the most one visit may count for; other events count once.
        capped = case((visits.c.type == "dwell", case((visits.c.total > 600.0, 600.0), else_=visits.c.total)),
                      else_=case((visits.c.mx > 1.0, 1.0), else_=visits.c.mx))
        out: dict[int, float] = {}
        for article_id, etype, value in conn.execute(
            select(visits.c.article_id, visits.c.type, func.sum(capped)).group_by(visits.c.article_id, visits.c.type)
        ).all():
            w = EVENT_WEIGHTS.get(etype, 0.0)
            out[article_id] = out.get(article_id, 0.0) + w * float(value or 0.0)
        return out

    # Recounted in full once a day; in between, only articles whose count can have changed: those
    # with events recorded since the last count (a reader's event time may be up to a day earlier,
    # see the events guard) and those whose events have left the 30-day window since.
    now = db.utcnow()
    state = cache.ENGAGEMENT.get(conn)
    last, full_at = state.get("at"), float(state.get("full_at") or 0)
    if last is None or now.timestamp() - full_at >= ENGAGEMENT_FULL_HOURS * 3600:
        raw = count()
        full_at = now.timestamp()
    else:
        last_dt = datetime.fromtimestamp(float(last), tz=timezone.utc)
        changed = [r[0] for r in conn.execute(
            select(e.article_id).distinct().where(e.article_id.isnot(None), or_(
                e.created_at >= last_dt - timedelta(hours=LATE_EVENT_HOURS),
                and_(e.created_at >= last_dt - timedelta(days=30, hours=1), e.created_at < since)))).all()]
        raw = {int(k): float(v) for k, v in (state.get("raw") or {}).items()}
        for aid in changed:
            raw.pop(aid, None)
        for i in range(0, len(changed), 500):
            raw.update(count(e.article_id.in_(changed[i : i + 500])))
    cache.ENGAGEMENT.put(conn, {"at": now.timestamp(), "full_at": full_at, "raw": {str(k): v for k, v in raw.items()}})
    return raw


def popularity(points: int | None, trend: int | None) -> float:
    """External popularity on a log scale: 0 for nothing, ~5 for a big HN thread."""
    return math.log1p(max(points or 0, 0)) + 0.6 * math.log1p(max(trend or 0, 0))


# Community traction: a bounded, log-scaled bonus for the story's biggest discussion thread (Hacker
# News points). Before it, points only reached the score through velocity (popularity per hour,
# worth 0.10 at most and gone after a day) and the learned prediction, so a 676-point thread with
# seven publishers scored 0.38 and sat below single-outlet stories. Points up to TRACTION_FLOOR add
# nothing (a 20-point thread is noise), the bonus grows with log10 of the points above that and
# reaches TRACTION_WEIGHT at TRACTION_FULL points, and never more.
TRACTION_WEIGHT = 0.15
TRACTION_FLOOR = 30
TRACTION_FULL = 1000


def traction(points: int | None) -> float:
    """0..1: min(1, max(0, log10(1 + p) - log10(1 + FLOOR)) / (log10(1 + FULL) - log10(1 + FLOOR)))."""
    p = max(int(points or 0), 0)
    if p <= TRACTION_FLOOR:
        return 0.0
    lo = math.log10(1 + TRACTION_FLOOR)
    return min(1.0, (math.log10(1 + p) - lo) / (math.log10(1 + TRACTION_FULL) - lo))


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
    # The runner's copy of the articles and their embeddings (cache.py): only articles written since
    # the previous run are read, instead of 30 days of embeddings (~8.5 KB each) on every run.
    arts = sorted((a for a in cache.articles(conn).values()
                   if a.status == "published" and (db.as_utc(a.created_at) or now) >= since and a.len_embedding >= 0),
                  key=lambda a: a.id)
    vecs = cache.article_vectors(conn, arts) if arts else {}
    dims = [len(v) for v in vecs.values() if v is not None]
    dim = max(set(dims), key=dims.count) if dims else 0
    arts = [a for a in arts if vecs.get(a.id) is not None and len(vecs[a.id]) == dim]
    if not arts:
        return stats
    story_size: dict[int, int] = {}
    story_primary: dict[int, bool] = {}
    for a in arts:
        story_size[a.story_id] = story_size.get(a.story_id, 0) + 1
        if a.domain in config.PRIMARY_DOMAINS:
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

    def row(a) -> np.ndarray:
        return np.concatenate([vecs[a.id].astype(np.float64),
                               np.asarray(_features(a, story_size[a.story_id], story_primary.get(a.story_id, False)))])

    train = engaged if use_engagement else [a for a in arts if y_by_id[a.id] > 0]
    can_train = len(train) >= MIN_TRAINING_ARTICLES // 2
    # The model is refitted once a day (RANK_TRAIN_HOURS) rather than every run: a refit nudges almost every
    # prediction, and each rewritten article is read again by the next run (cache.py). Between
    # refits, new articles and articles whose features changed get predictions from the last fit.
    model = cache.RANK_MODEL.get(conn)
    due = (now.timestamp() - float(model.get("trained_at") or 0) >= config.RANK_TRAIN_HOURS * 3600
           or model.get("target") != stats["target"] or model.get("dim") != dim
           or (model.get("weights") is not None) != can_train)
    if due:
        model = {"target": stats["target"], "dim": dim, "trained_at": now.timestamp(), "weights": None}
        if can_train:
            X = np.stack([row(a) for a in train])
            y = np.asarray([y_by_id[a.id] for a in train], dtype=np.float64)
            y_mean = y.mean()
            XtX = X.T @ X + RIDGE_LAMBDA * np.eye(X.shape[1])
            weights = np.linalg.solve(XtX, X.T @ (y - y_mean))
            model.update(weights=weights.tolist(), y_mean=float(y_mean), y_max=max(float(y.max()), 1e-6), training_rows=len(train))
        cache.RANK_MODEL.put(conn, model)
    weights = np.asarray(model["weights"], dtype=np.float64) if model.get("weights") is not None else None
    stats.update(trained=weights is not None, training_rows=int(model.get("training_rows") or 0), refitted=due,
                 model_age_hours=round((now.timestamp() - float(model["trained_at"])) / 3600, 1))

    pred_rows: list[dict] = []
    for a in arts:
        if weights is not None:
            pred = float(row(a) @ weights) + model["y_mean"]
            pred = max(0.0, min(1.0, pred / model["y_max"]))
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
    stories_rows = sorted((s for s in cache.stories(conn).values() if (db.as_utc(s.updated_at) or now) >= since), key=lambda s: s.id)
    ids = {s.id for s in stories_rows}
    source_type = dict(conn.execute(select(db.sources.c.id, db.sources.c.source_type)).all())
    members_by_story: dict[int, list] = {}
    for m in sorted(cache.articles(conn).values(), key=lambda a: a.id):
        if m.status == "published" and m.story_id in ids:
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
        breaking = any((m.content_type in (None, "news", "product", "research")) and source_type.get(m.source_id) != "newsletter"
                       for m in members)
        # Practical guides are the point of a focus category, so they keep full freshness there.
        focus = config.FOCUS_CATEGORIES.get(s.category or "", 0.0)
        recency_weight = 1.0 if (breaking or focus) else 0.55
        breadth = min(1.0, math.log1p(len(members)) / math.log(6))
        pop = max(popularity(m.discussion_points, m.trend_score) for m in members)
        age_h = max(1.0, (now - first).total_seconds() / 3600)
        velocity = min(1.0, (pop / 6.0) * (24.0 / max(age_h, 6.0)))  # popularity gained fast counts more
        # Freshness belongs to when the story broke. A new article on a days-old story is a
        # development, worth at most half the freshness of genuinely new news.
        freshness = max(_recency(first, now), 0.5 * _recency(latest, now))
        points = max((m.discussion_points or 0) for m in members)
        score = round(
            0.30 * predicted
            + 0.20 * (s.importance or 5) / 10.0
            + 0.22 * freshness * recency_weight
            + 0.10 * breadth
            + 0.10 * velocity
            + 0.08 * min(1.0, math.log1p(engagement) / 6.0)
            + TRACTION_WEIGHT * traction(points)
            + focus,
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
    rows = [a for a in sorted(cache.articles(conn).values(), key=lambda a: a.id)
            if a.status == "published" and db.as_utc(a.created_at) >= since]
    if not rows:
        return
    per_source: dict[int, list[float]] = {}
    for a in rows:
        per_source.setdefault(a.source_id, []).append(float(a.engagement or 0.0) + popularity(a.discussion_points, a.trend_score))
    means = {sid: sum(v) / len(v) for sid, v in per_source.items()}
    overall = sum(means.values()) / len(means)
    if overall <= 0:
        return
    # One batched statement for every source, not a round trip each.
    conn.execute(update(db.sources).where(db.sources.c.id == bindparam("sid"))
                 .values(engagement_ema=db.sources.c.engagement_ema * 0.7 + bindparam("rel") * 0.3),
                 [{"sid": sid, "rel": min(2.0, avg / overall)} for sid, avg in means.items()])


def is_discovery_term(term: str) -> bool:
    term = (term or "").strip()
    return 2 < len(term) < 40 and not GENERIC_TERM.search(term)


def discovery_key(term: str) -> str:
    return f"discover-{_DASHES.sub('-', term.strip().lower()).replace(' ', '-')[:50]}"


def discovery_terms(top: list, min_articles: int = config.DISCOVERY_MIN_ARTICLES, limit: int = MAX_DISCOVERED_SOURCES) -> list[str]:
    """Company and model names shared by the best-performing articles, best first.
    No headline keywords: generic phrases made search feeds that returned anything."""
    counts: dict[str, float] = {}
    articles: dict[str, int] = {}
    display: dict[str, str] = {}
    for a in top:
        ents = a.entities or {}
        names = list(ents.get("companies") or []) + list(ents.get("models") or [])
        weight = math.log1p(float(a.engagement or 0.0) + 3.0 * popularity(a.discussion_points, a.trend_score))
        seen: set[str] = set()
        for t in names:
            t = _DASHES.sub("-", str(t)).strip()
            k = t.lower()
            if k in seen or not is_discovery_term(t):
                continue
            seen.add(k)
            counts[k] = counts.get(k, 0.0) + weight
            articles[k] = articles.get(k, 0) + 1
            display.setdefault(k, t)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [display[k] for k, _ in ranked if articles[k] >= min_articles][:limit]


def cap_discovered_sources(conn, preferred_keys: list[str] | tuple = ()) -> int:
    """Disable discovered feeds beyond MAX_DISCOVERED_SOURCES, and any whose term is generic.
    Keeps the preferred keys first (this run's picks, best first), then the newest."""
    rows = conn.execute(
        select(db.sources.c.id, db.sources.c.key, db.sources.c.name, db.sources.c.expires_at)
        .where(db.sources.c.discovered.is_(True), db.sources.c.enabled.is_(True))
    ).all()
    pref = {k: i for i, k in enumerate(preferred_keys)}
    drop = [r.id for r in rows if not is_discovery_term((r.name or "").removeprefix("Search: "))]
    alive = [r for r in rows if r.id not in drop]
    alive.sort(key=lambda r: (pref.get(r.key, len(pref)), -(db.as_utc(r.expires_at).timestamp() if r.expires_at else 0.0)))
    drop += [r.id for r in alive[MAX_DISCOVERED_SOURCES:]]
    if drop:
        conn.execute(update(db.sources).where(db.sources.c.id.in_(drop)).values(enabled=False))
    return len(drop)


def discover(conn) -> int:
    """Turn the best-performing companies and models into temporary Bing News search feeds."""
    since = db.utcnow() - timedelta(days=7)
    rows = [a for a in sorted(cache.articles(conn).values(), key=lambda a: a.id)
            if a.status == "published" and db.as_utc(a.created_at) >= since]
    scored = [(float(a.engagement or 0.0) + 3.0 * popularity(a.discussion_points, a.trend_score), a) for a in rows]
    top = [a for s, a in sorted(scored, key=lambda x: -x[0]) if s > 0][:25]
    text = cache.article_text(conn, top)  # entities of the 25 best only
    top = [cache.merged(a, text.get(a.id)) for a in top if a.id in text]
    if len(top) < 5:
        cap_discovered_sources(conn)
        return 0
    ranked = discovery_terms(top)
    created = 0
    for term in ranked:
        key = discovery_key(term)
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
    cap_discovered_sources(conn, [discovery_key(t) for t in ranked])
    return created


def run() -> dict:
    eng = db.engine()
    with eng.begin() as conn:
        stats = train_and_predict(conn)
        stats["stories_scored"] = score_stories(conn)
        update_source_weights(conn)
        stats["discovered_sources"] = discover(conn)
    return stats
