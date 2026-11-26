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
- Oversized stories. Before CLUSTER_MAX_TOTAL, chained merges and unbounded overflow grew one story
  to 161 sources; members furthest from its centroid are detached to be clustered again.
- Round-ups and late leads. Until 26 November a round-up of the week's news could lead a story, or
  form one, and an article days after the event could take a story's lead and headline (cluster.py
  and merge.py now prevent both). A round-up member stops being shown (overflow), a story made of
  round-ups only is unpublished, and a story whose lead is a round-up or long after its coverage
  gets the lead merge.pick_lead chooses, with that article's headline and digest.

Unlike tidy.py these changes are what the pages show, so they must look changed to the runner's
copy (cache.py): no revision_kept here. Every query is narrow (ids, addresses, lengths) and capped,
so a run reads at most a few hundred short rows for each repair.
"""
from __future__ import annotations

import logging

from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

from . import cache, cluster, config, db, merge
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
OVERSIZED_STORIES_PER_RUN = 3
LEADS_PER_RUN = 20


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


def oversized_stories(eng, max_total: int | None = None, limit: int | None = None, max_stories: int = OVERSIZED_STORIES_PER_RUN) -> dict[str, int]:
    """Stories holding more members (shown and overflow) than CLUSTER_MAX_TOTAL, grown before the
    ceiling existed by chained merges and unbounded overflow (one had 161 sources, cluster.py). The
    lead and the members closest to the story's centroid stay, up to the ceiling; the rest are
    detached, least similar first: story_id cleared, status back to "enriched" with their embedding
    and digest kept, so the next cluster step places them again. Their old story's window has
    closed, so they form stories of their own (or join a fresh one on the same event) instead of
    returning. The members and their embeddings come from the runner's copy (cache.py); a run with
    no oversized story reads nothing, and at most `limit` articles move per run."""
    max_total = max_total or config.CLUSTER_MAX_TOTAL
    limit = config.CLUSTER_REPAIR_MAX_PER_RUN if limit is None else limit
    done = {"stories": 0, "detached": 0}
    with eng.begin() as conn:
        members_of: dict[int, list] = {}
        for m in cache.articles(conn).values():
            if m.story_id and m.status in ("published", "overflow"):
                members_of.setdefault(m.story_id, []).append(m)
        big = sorted((sid for sid, ms in members_of.items() if len(ms) > max_total), key=lambda sid: (-len(members_of[sid]), sid))
        if not big:
            return {}
        stories = cache.stories(conn)
        for sid in big[:max_stories]:
            s = stories.get(sid)
            budget = limit - done["detached"]
            if s is None or s.status != "published" or budget <= 0:
                continue
            members = sorted(members_of[sid], key=lambda m: m.id)
            vecs = cache.article_vectors(conn, members)
            ids = {m.id for m in members}
            lead_id = s.lead_article_id if s.lead_article_id in ids else max(members, key=lambda m: (m.importance or 0, -m.id)).id
            keep, detach = cluster.split_by_centroid(
                lead_id, [(m.id, vecs.get(m.id), m.status == "published") for m in members], max_total)
            detach = detach[:budget]
            if not detach:
                continue
            conn.execute(update(db.articles).where(db.articles.c.id.in_(detach)).values(story_id=None, status="enriched"))
            left = [m for m in members if m.id not in set(detach)]
            values = {"article_count": len(left)}
            shown = [vecs[m.id] for m in left if m.status == "published" and vecs.get(m.id) is not None]
            dims = {v.shape for v in shown}
            if len(dims) == 1:
                mean = cluster._mean_vec(shown)  # noqa: SLF001 - the story's embedding is the mean of its shown members
                if mean is not None:
                    values["embedding"] = db.pack_vec(mean)
            conn.execute(update(db.stories).where(db.stories.c.id == sid).values(**values))
            done["stories"] += 1
            done["detached"] += len(detach)
            log.info("story #%s: %d members, detached the %d furthest from its centroid to be clustered again",
                     sid, len(members), len(detach))
    return {k: v for k, v in done.items() if v}


def story_leads(eng, limit: int = LEADS_PER_RUN) -> dict[str, int]:
    """Published stories of the merge window (MERGE_LOOKBACK_DAYS) with a round-up among their shown
    members, or a lead published long after their coverage. Round-up members become overflow
    (counted, not shown); a story with nothing else is unpublished; a story whose lead was a
    round-up or late gets the lead merge.pick_lead chooses among the rest, and its headline and digest.
    Members and titles come from the runner's copy (cache.py), which the export reads anyway; at most
    `limit` stories change per run."""
    done = {"roundups_hidden": 0, "stories_unpublished": 0, "leads_changed": 0}
    now = db.utcnow()
    since = now - timedelta(days=config.MERGE_LOOKBACK_DAYS)
    with eng.begin() as conn:
        stories = {s.id: s for s in cache.stories(conn).values()
                   if s.status == "published" and (db.as_utc(s.first_published_at) or now) >= since}
        members_of: dict[int, list] = {}
        for m in cache.articles(conn).values():
            if m.story_id in stories and m.status == "published":
                members_of.setdefault(m.story_id, []).append(m)
        texts = cache.article_text(conn, [m for ms in members_of.values() for m in ms])
        source_type = None
        changed = 0
        for sid in sorted(members_of):
            if changed >= limit:
                break
            s, shown = stories[sid], sorted((m for m in members_of[sid] if m.id in texts), key=lambda m: m.id)
            if not shown:
                continue
            roundups = [m for m in shown if merge.is_roundup(texts[m.id].title)]
            rest = [m for m in shown if m not in roundups]
            lead = next((m for m in shown if m.id == s.lead_article_id), None)
            if not rest:
                conn.execute(update(db.stories).where(db.stories.c.id == sid).values(status="unpublished", updated_at=now))
                done["stories_unpublished"] += 1
                changed += 1
                log.info("story #%s: every source is a round-up of several stories; unpublished", sid)
                continue
            middle = merge.coverage_time(rest)
            late = (lead is not None and middle is not None and db.as_utc(lead.published_at) is not None
                    and db.as_utc(lead.published_at) - middle > timedelta(hours=merge.LEAD_LATE_HOURS))
            if not roundups and not late:
                continue
            if source_type is None:
                source_type = dict(conn.execute(select(db.sources.c.id, db.sources.c.source_type)).all())
            primary = lambda m: m.domain in config.PRIMARY_DOMAINS or source_type.get(m.source_id) == "primary"  # noqa: E731
            new = merge.pick_lead(rest, texts, primary)
            if not roundups and (new is None or new.id == s.lead_article_id):
                continue  # late, but no better lead among the rest: nothing to write, now or next run
            if roundups:
                conn.execute(update(db.articles).where(db.articles.c.id.in_([m.id for m in roundups])).values(status="overflow"))
                done["roundups_hidden"] += len(roundups)
            values = {"updated_at": now}
            if new is not None and new.id != s.lead_article_id:
                t = texts[new.id]
                values.update(lead_article_id=new.id, headline=t.headline or t.title, summary_md=t.summary_md,
                              key_points=t.key_points, why_it_matters=t.why_it_matters, entities=t.entities,
                              source_notes=None, **({"category": new.category} if new.category else {}))
                done["leads_changed"] += 1
                log.info("story #%s: lead #%s (%s) replaced by #%s: %s", sid, s.lead_article_id,
                         "a round-up" if lead in roundups else "long after its coverage", new.id, values["headline"])
            conn.execute(update(db.stories).where(db.stories.c.id == sid).values(**values))
            changed += 1
    return {k: v for k, v in done.items() if v}


def run() -> dict:
    eng = db.engine()
    stats: dict = {}
    for name, fn in (("bing_links", bing_links), ("teasers", teasers), ("oversized_stories", oversized_stories),
                     ("story_leads", story_leads)):
        try:
            done = fn(eng)
        except Exception as exc:  # noqa: BLE001 - a repair that fails waits for the next run
            log.warning("%s repair failed: %s", name, str(exc)[:160])
            done = {"failed": True}
        if done:
            stats[name] = done
    return stats
