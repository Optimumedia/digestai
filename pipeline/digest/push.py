"""Step: browser push alerts for breaking stories (Web Push, VAPID, no third-party service).

Readers subscribe from the site (service worker + PushManager); the browser's push service
endpoint and keys land in `push_subscriptions` through the same anonymous insert path as
reader events. This step picks the one story most worth interrupting people for, sends it to
every subscription, prunes endpoints the push service reports gone, and records the push so a
story is never sent twice. Quiet by design: at most PUSH_MAX_PER_DAY a day and one per run.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from . import config, db

log = logging.getLogger("digest.push")

MAX_PER_RUN = 1
FRESH_HOURS = 4          # only stories first seen recently qualify
MIN_ARTICLES = 2         # at least two outlets, or one primary source with high importance
MIN_IMPORTANCE = 7


def _candidates(conn, now):
    since = now - timedelta(hours=FRESH_HOURS)
    rows = conn.execute(
        select(db.stories.c.id, db.stories.c.slug, db.stories.c.headline, db.stories.c.key_points,
               db.stories.c.summary_md, db.stories.c.importance, db.stories.c.score, db.stories.c.article_count)
        .where(db.stories.c.status == "published", db.stories.c.pushed_at.is_(None), db.stories.c.first_published_at >= since)
        .order_by(db.stories.c.score.desc())
    ).all()
    out = []
    for r in rows:
        imp = r.importance or 0
        if (r.article_count or 0) >= MIN_ARTICLES and imp >= MIN_IMPORTANCE - 1:
            out.append(r)
        elif imp >= MIN_IMPORTANCE + 1:
            out.append(r)
    return out


def _payload(story) -> str:
    points = story.key_points
    if isinstance(points, str):
        try:
            points = json.loads(points)
        except ValueError:
            points = []
    body = (points[0] if points else (story.summary_md or "")).strip()
    body = body.split("\n")[0][:140]
    return json.dumps({
        "title": story.headline[:100],
        "body": body,
        "url": f"{config.SITE_URL}/story/{story.slug}?source=push",
        "tag": f"story-{story.id}",
        "icon": f"{config.SITE_URL}/logo-192.png",
        "image": f"{config.SITE_URL}/og/story-{story.slug}.png",
    })


def run() -> dict:
    stats = {"sent": 0, "subscribers": 0, "pruned": 0, "skipped": ""}
    if not (config.VAPID_PRIVATE_KEY and config.PUBLIC_VAPID_KEY):
        stats["skipped"] = "no VAPID keys"
        return stats
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        stats["skipped"] = "pywebpush not installed"
        return stats

    eng = db.engine()
    now = db.utcnow()
    with eng.connect() as conn:
        subs = conn.execute(select(db.push_subscriptions)).all()
        stats["subscribers"] = len(subs)
        if not subs:
            return stats
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = conn.execute(select(func.count()).select_from(db.stories).where(db.stories.c.pushed_at >= day_start)).scalar() or 0
        if sent_today >= config.PUSH_MAX_PER_DAY:
            stats["skipped"] = "daily cap reached"
            return stats
        cands = _candidates(conn, now)
    if not cands:
        stats["skipped"] = "nothing breaking"
        return stats

    claims = {"sub": f"{config.SITE_URL}/about"}
    for story in cands[:MAX_PER_RUN]:
        data = _payload(story)
        gone: list[int] = []
        failed: list[int] = []
        delivered = 0
        for s in subs:
            info = {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}}
            try:
                webpush(subscription_info=info, data=data, vapid_private_key=config.VAPID_PRIVATE_KEY,
                        vapid_claims=dict(claims), ttl=6 * 3600, timeout=10)
                delivered += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    gone.append(s.id)
                else:
                    failed.append(s.id)
                    log.warning("push failed (%s): %s", status, str(exc)[:120])
            except Exception as exc:  # noqa: BLE001
                failed.append(s.id)
                log.warning("push error: %s", str(exc)[:120])
        with eng.begin() as conn:
            conn.execute(update(db.stories).where(db.stories.c.id == story.id).values(pushed_at=now))
            if gone:
                conn.execute(delete(db.push_subscriptions).where(db.push_subscriptions.c.id.in_(gone)))
            if failed:
                conn.execute(update(db.push_subscriptions).where(db.push_subscriptions.c.id.in_(failed))
                             .values(failures=db.push_subscriptions.c.failures + 1))
                conn.execute(delete(db.push_subscriptions).where(db.push_subscriptions.c.failures >= 5))
            if delivered:
                conn.execute(update(db.push_subscriptions).where(db.push_subscriptions.c.failures == 0).values(last_ok_at=now))
        stats["sent"] += delivered
        stats["pruned"] += len(gone)
        stats["story"] = story.slug
        log.info("pushed %r to %d of %d subscribers", story.headline[:60], delivered, len(subs))
    return stats
