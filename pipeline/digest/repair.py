"""Step: one-time repairs of stored rows, a bounded batch per run until none are left.

Two mistakes from before their fixes are still in the database and on the pages:

- Bing click links. Until 14 September, Bing News items were stored with their
  bing.com/news/apiclick.aspx address (normalize_url now unwraps it for new items), so the RSS feed
  linked bing.com, stories showed "bing.com" as a source, and the same article, fetched with a new
  tracking id each time, sat in one story several times. Each stored link is unwrapped to the real
  address and domain; when that address already belongs to another article, the Bing copy is
  rejected as its duplicate (reject_reason "repair: duplicate of #id").
- Paywall teasers. A subscription wall's pitch ("Subscribe to unlock this article...") was stored as
  the article's text and shown as its full text (extract.teaser_reason now catches it). Those rows
  stop showing their text; a story left with no other readable source is unpublished.

Unlike tidy.py these changes are what the pages show, so they must look changed to the runner's
copy (cache.py): no revision_kept here. Every query is narrow (ids, addresses, lengths) and capped,
so a run reads at most a few hundred short rows for each repair.
"""
from __future__ import annotations

import logging

from sqlalchemy import and_, func, or_, select, update

from . import db
from .extract import TEASER_MAX_WORDS
from .textutil import domain_of, normalize_url

log = logging.getLogger("digest.repair")

BING_ROWS_PER_RUN = 300
TEASER_ROWS_PER_RUN = 300
BING_LINK = "%bing.com/news/apiclick.aspx%"
# Phrases a wall's pitch always carries; matched in the database so only ids come back.
TEASER_PHRASES = ["Subscribe to unlock", "Subscribe to read", "undefined now undefined", "was undefined",
                  "Sign in to read", "Already a subscriber", "Start your free trial", "subscribers only",
                  "Try unlimited access", "Complete digital access"]
LIVE_STATUSES = ("extracted", "gated", "enriched", "published")
READABLE_DESCRIPTION_CHARS = 200  # about 40 words: what extract.py accepts as a digest-only article


def bing_links(eng, limit: int = BING_ROWS_PER_RUN) -> dict[str, int]:
    """Unwrap at most `limit` stored Bing click links; returns what happened to them."""
    a = db.articles.c
    done = {"unwrapped": 0, "duplicates": 0, "unwrappable": 0}
    with eng.begin() as conn:
        rows = conn.execute(select(a.id, a.url).where(a.url.like(BING_LINK), a.status != "rejected")
                            .order_by(a.id).limit(limit)).all()
        for r in rows:
            try:
                url = normalize_url(r.url)
            except ValueError:
                url = r.url
            dom = domain_of(url)
            if url == r.url or not dom or dom == "bing.com":
                # No article address inside the link: nothing a page could show or link to.
                conn.execute(update(db.articles).where(a.id == r.id)
                             .values(status="rejected", reject_reason="repair: bing link could not be unwrapped"))
                done["unwrappable"] += 1
                continue
            other = conn.execute(select(a.id).where(a.url == url, a.id != r.id)).first()
            if other:
                conn.execute(update(db.articles).where(a.id == r.id)
                             .values(status="rejected", reject_reason=f"repair: duplicate of #{other.id}"[:200]))
                done["duplicates"] += 1
            else:
                conn.execute(update(db.articles).where(a.id == r.id).values(url=url, domain=dom[:200]))
                done["unwrapped"] += 1
    return {k: v for k, v in done.items() if v}


def teasers(eng, limit: int = TEASER_ROWS_PER_RUN) -> dict[str, int]:
    """Stop showing at most `limit` stored paywall pitches as article text; unpublish stories that
    have no other readable source. Rows fixed leave the query, so each run takes the next batch."""
    a, s = db.articles.c, db.stories.c
    done = {"hidden": 0, "stories_unpublished": 0}
    with eng.begin() as conn:
        rows = conn.execute(
            select(a.id, a.story_id)
            .where(a.status.in_(LIVE_STATUSES), a.show_fulltext.is_(True), a.word_count < TEASER_MAX_WORDS,
                   or_(*[a.content_md.ilike(f"%{p}%") for p in TEASER_PHRASES]))
            .order_by(a.id).limit(limit)).all()
        if not rows:
            return {}
        ids = [r.id for r in rows]
        conn.execute(update(db.articles).where(a.id.in_(ids)).values(show_fulltext=False, extraction_ok=False))
        done["hidden"] = len(ids)
        story_ids = sorted({r.story_id for r in rows if r.story_id})
        if story_ids:
            # Another published article with text to show, or a feed description long enough to be
            # the digest, keeps the story; otherwise its only source was the pitch.
            readable = {r.story_id for r in conn.execute(
                select(a.story_id).where(
                    a.story_id.in_(story_ids), a.status == "published", ~a.id.in_(ids),
                    or_(and_(a.show_fulltext.is_(True), func.coalesce(func.length(a.content_md), 0) > 0),
                        func.coalesce(func.length(a.description), 0) >= READABLE_DESCRIPTION_CHARS))).all()}
            bare = [sid for sid in story_ids if sid not in readable]
            if bare:
                n = conn.execute(update(db.stories).where(s.id.in_(bare), s.status == "published")
                                 .values(status="unpublished")).rowcount or 0
                done["stories_unpublished"] = n
    return {k: v for k, v in done.items() if v}


def run() -> dict:
    eng = db.engine()
    stats: dict = {}
    for name, fn in (("bing_links", bing_links), ("teasers", teasers)):
        try:
            done = fn(eng)
        except Exception as exc:  # noqa: BLE001 - a repair that fails waits for the next run
            log.warning("%s repair failed: %s", name, str(exc)[:160])
            done = {"failed": True}
        if done:
            stats[name] = done
    return stats
