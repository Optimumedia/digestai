"""One story per event: which article leads a story, when two stories are the same event, and
merging the newer story into the older one (its old address then redirects there).

Duplicate stories came from the cluster cap: once a story held CLUSTER_MAX_ARTICLES, every further
article on the event started a new story (seventeen pages for one funding round). The cluster step
now attaches such articles as overflow (cluster.py); this module merges the duplicates that already
exist and any that still slip through, a bounded number per run, and lists the pairs just under
the bar for review on the dashboard (quality.py). Everything it compares comes from the runner's
copy (cache.py): the story vectors of the threads window and the story and article texts the
export reads anyway, so a run without duplicates reads nothing extra.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from difflib import SequenceMatcher

import numpy as np
from sqlalchemy import select, update

from . import cache, config, db

log = logging.getLogger("digest.merge")

MERGED = "merged"           # stories.status of a story folded into another (redirect_to points there)
STRONG_MARGIN = 0.05        # this far above the bar, two stories are one event even without a shared name
SUSPECT_MARGIN = 0.06       # this far below it, with a shared name, a pair is listed for review
MAX_SUSPECTS = 20
LEAD_IMPORTANCE_MARGIN = 2  # a new lead must beat the current one by this much to change the headline

STOP = set("a an the and or of to in on for with by at from as is are be its it this that new says said after over into how why what".split())


# ---------------------------------------------------------------------------- lead choice

# Commentary describes an event second-hand: it may lead a story only while no news article is in it.
NOT_NEWS_TYPES = {"opinion", "analysis", "newsletter", "podcast", "tutorial", "listicle"}
OPINION_START = re.compile(r"^(?:why|how|what|should|could|can|will|is|are|do|does|opinion|column|i|i'm|i've|we|we're|we've|my|our|let's|here's)\b", re.I)
# "I" only as a word (not the I in "A.I."), plus the contractions and "my" in lower case.
FIRST_PERSON = re.compile(r"(?<![\w.'])(?:I|I'm|I've|I'd)(?![\w.'])|\bmy\b")


def is_news(content_type: str | None) -> bool:
    return (content_type or "news") not in NOT_NEWS_TYPES


def opinion_headline(headline: str | None) -> bool:
    """A question, a first-person piece or a why/how explainer: never a story's headline while news exists."""
    h = (headline or "").strip()
    return bool(h) and (h.endswith("?") or bool(OPINION_START.match(h)) or bool(FIRST_PERSON.search(h)))


def news_ok(headline: str | None, content_type: str | None) -> bool:
    return is_news(content_type) and not opinion_headline(headline)


def better_lead(new: dict, cur: dict) -> bool:
    """Whether an article just merged into a story replaces its lead (the lead's headline and digest
    are the story's). News beats commentary and commentary never displaces news; a primary source
    leads when it is at least as important; anything else must be clearly more important.
    Both: headline, content_type, importance, primary."""
    n_ok, c_ok = news_ok(new.get("headline"), new.get("content_type")), news_ok(cur.get("headline"), cur.get("content_type"))
    if n_ok != c_ok:
        return n_ok
    ni, ci = new.get("importance") or 0, cur.get("importance") or 0
    if n_ok and new.get("primary") and not cur.get("primary"):
        return ni >= ci
    return ni >= ci + LEAD_IMPORTANCE_MARGIN


def headline_tokens(text: str | None) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split() if t not in STOP]


def coverage_share(tokens: list[str], titles: list[list[str]]) -> float:
    """How many of the titles share two or more words with these tokens, as a fraction."""
    if not titles or len(tokens) < 2:
        return 0.0
    mine = set(tokens)
    return sum(1 for t in titles if len(mine & set(t)) >= 2) / len(titles)


def pick_lead(members: list, texts: dict, primary) -> object | None:
    """The member whose digest describes a story: news over commentary, then importance with a point
    for a primary source and up to a point for wording the other sources share, then the earliest.
    members: mirror article rows; texts: their text rows (cache.article_text); primary(row) -> bool."""
    shown = [m for m in members if m.status == "published" and m.id in texts]
    if not shown:
        return None
    titles = [headline_tokens(texts[m.id].title or texts[m.id].headline) for m in shown]

    def key(m):
        t = texts[m.id]
        head = t.headline or t.title
        return (news_ok(head, m.content_type),
                (m.importance or 0) + (1 if primary(m) else 0) + coverage_share(headline_tokens(head), titles), -m.id)

    return max(shown, key=key)


# ---------------------------------------------------------------------------- same event

def _tokens_similarity(ta: list[str], tb: list[str]) -> float:
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    if jac < 0.4:
        return jac
    return max(jac, SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio())


def headline_similarity(a: str | None, b: str | None) -> float:
    return _tokens_similarity(headline_tokens(a), headline_tokens(b))


def entity_names(entities) -> set[str]:
    names = set()
    if not isinstance(entities, dict):
        return names
    for kind in ("companies", "models", "people"):
        for n in entities.get(kind) or []:
            key = re.sub(r"[^a-z0-9]+", " ", str(n).lower()).strip()
            if key:
                names.add(key)
    return names


def shared_entities(a, b) -> list[str]:
    return sorted(entity_names(a) & entity_names(b))


def facts_of(headline, entities, at) -> dict:
    """What same_event compares, with the headline's tokens and the entity names worked out once:
    find_pairs asks about every pair of stories in the window, so per-pair tokenising was the
    whole cost of the cluster step (fifty seconds over a thousand stories)."""
    tokens = headline_tokens(headline)
    return {"headline": headline, "entities": entities, "at": at,
            "tokens": tokens, "sorted": sorted(tokens), "names": entity_names(entities)}


def _facts(x: dict) -> dict:
    return x if "tokens" in x else facts_of(x.get("headline"), x.get("entities"), x.get("at"))


def same_event(a: dict, b: dict, sim: float | None, thr: float) -> str | None:
    """Why two stories are one event, or None. a and b: headline, entities, at (when each broke, a
    datetime or None), or the same prepared by facts_of. sim: the cosine similarity of their
    embeddings, or None when unknown (the exported data carries none): then the wording and the
    names have to give it away."""
    if a.get("at") and b.get("at") and abs(a["at"] - b["at"]) > timedelta(days=config.MERGE_PAIR_DAYS):
        return None
    a, b = _facts(a), _facts(b)
    if len(a["tokens"]) >= 3 and a["sorted"] == b["sorted"]:
        return "the same headline"
    shared = sorted(a["names"] & b["names"])
    if not config.MERGE_DUPLICATES:
        # Wording only: two feeds carrying the same piece, or the same headline reworded slightly.
        if shared and _tokens_similarity(a["tokens"], b["tokens"]) >= 0.85:
            return f"nearly the same headline, both about {shared[0].title()}"
        return None
    if sim is not None:
        if sim >= thr and shared:
            return f"{sim:.2f} alike, both about {shared[0].title()}"
        if sim >= thr + STRONG_MARGIN:
            return f"{sim:.2f} alike"
        return None
    if shared and _tokens_similarity(a["tokens"], b["tokens"]) >= 0.6:
        return f"nearly the same headline, both about {shared[0].title()}"
    return None


def find_pairs(stories: list, vecs: dict, thr: float) -> tuple[list[tuple], list[tuple]]:
    """Among stories (id, headline, entities, first_published_at) with their vectors: the pairs that
    are one event, most alike first, and the pairs just under the bar. Each is (sim, reason, a_id, b_id)."""
    facts = {s.id: facts_of(s.headline, s.entities, db.as_utc(s.first_published_at)) for s in stories}
    with_vec = [s.id for s in stories if vecs.get(s.id) is not None]
    dims = [vecs[i].shape[0] for i in with_vec]
    dim = max(set(dims), key=dims.count) if dims else 0
    with_vec = [i for i in with_vec if vecs[i].shape[0] == dim]
    sims: dict[tuple[int, int], float] = {}
    if len(with_vec) >= 2:
        m = np.stack([vecs[i] for i in with_vec]).astype(np.float32)
        s = m @ m.T
        for x in range(len(with_vec)):
            for y in range(x + 1, len(with_vec)):
                sims[(with_vec[x], with_vec[y])] = float(s[x, y])
    pairs, suspects = [], []
    ids = [s.id for s in stories]
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = ids[x], ids[y]
            sim = sims.get((a, b))
            reason = same_event(facts[a], facts[b], sim, thr)
            if reason:
                pairs.append((1.0 if sim is None else sim, reason, a, b))
            elif sim is not None and sim >= thr - SUSPECT_MARGIN:
                fa, fb = facts[a], facts[b]
                if fa["at"] and fb["at"] and abs(fa["at"] - fb["at"]) > timedelta(days=config.MERGE_PAIR_DAYS):
                    continue
                shared = sorted(fa["names"] & fb["names"])
                if shared:
                    suspects.append((sim, f"{sim:.2f} alike, both about {shared[0].title()}", a, b))
    pairs.sort(key=lambda p: (-p[0], p[2], p[3]))
    suspects.sort(key=lambda p: (-p[0], p[2], p[3]))
    return pairs, suspects


# ---------------------------------------------------------------------------- merging

def _mean(vec_a, n_a: int, vec_b, n_b: int):
    parts = [(v, n) for v, n in ((vec_a, n_a), (vec_b, n_b)) if v is not None]
    if not parts or len({v.shape for v, _n in parts}) != 1:
        return None
    m = sum(np.asarray(v, dtype=np.float32) * max(1, n) for v, n in parts) / sum(max(1, n) for _v, n in parts)
    norm = np.linalg.norm(m)
    return m / norm if norm else m


def merge_stories(conn, keep, drop, members: list, texts: dict, primary, vec_keep, vec_drop, now) -> dict:
    """Fold `drop` into `keep`: its articles move over, keep's lead is chosen again among every
    member, and drop stays as a signpost (status merged, redirect_to). keep and drop: story rows with
    their text (cache.merged); members: the articles of both (mirror rows); texts: the text of the
    shown ones (cache.article_text)."""
    conn.execute(update(db.articles).where(db.articles.c.story_id == drop.id).values(story_id=keep.id))
    lead = pick_lead(members, texts, primary)
    firsts = [db.as_utc(x) for x in (keep.first_published_at, drop.first_published_at) if x]
    values = {
        "article_count": len(members), "updated_at": now,
        "first_published_at": min(firsts) if firsts else now,
        "importance": max(keep.importance or 0, drop.importance or 0) or 5,
        "pinned": bool(keep.pinned or drop.pinned),
    }
    mean = _mean(vec_keep, keep.article_count or 1, vec_drop, drop.article_count or 1)
    if mean is not None:
        values["embedding"] = db.pack_vec(mean)
    if drop.thread_id and not keep.thread_id:
        values["thread_id"] = drop.thread_id
    elif drop.thread_id:
        # The thread lost an episode (it was keep's thread too, or another one).
        conn.execute(update(db.threads).where(db.threads.c.id == drop.thread_id)
                     .values(story_count=db.threads.c.story_count - 1))
    if not keep.pulse and drop.pulse:
        values.update(pulse=drop.pulse, pulse_at=now)
    changed_lead = lead is not None and lead.id != keep.lead_article_id
    if changed_lead:
        t = texts[lead.id]
        values.update(lead_article_id=lead.id, headline=t.headline or t.title, summary_md=t.summary_md, key_points=t.key_points,
                      why_it_matters=t.why_it_matters, entities=t.entities, importance=max(values["importance"], lead.importance or 0))
        if lead.category:
            values["category"] = lead.category
    conn.execute(update(db.stories).where(db.stories.c.id == keep.id).values(**values))
    conn.execute(update(db.stories).where(db.stories.c.id == drop.id)
                 .values(status=MERGED, redirect_to=keep.id, updated_at=now, pinned=False))
    return {"lead_changed": changed_lead, "articles": len(members), **{k: v for k, v in values.items() if k in ("headline", "thread_id")}}


def run(eng, thr: float, max_merges: int | None = None) -> dict:
    """Merge duplicate published stories of the last MERGE_LOOKBACK_DAYS, the older one staying,
    at most `max_merges` per run; then remember the pairs just under the bar for the dashboard."""
    max_merges = config.MERGE_MAX_PER_RUN if max_merges is None else max_merges
    stats = {"merged_stories": 0, "articles_moved": 0, "suspects": 0}
    now = db.utcnow()
    since = now - timedelta(days=config.MERGE_LOOKBACK_DAYS)
    with eng.begin() as conn:
        mirror = cache.stories(conn)
        rows = sorted((s for s in mirror.values()
                       if s.status == "published" and (db.as_utc(s.first_published_at) or now) >= since), key=lambda s: s.id)
        if len(rows) < 2:
            cache.SUSPECTS.put(conn, {"pairs": []})
            return stats
        vecs = cache.story_vectors(conn, [s for s in rows if (s.len_embedding or -1) >= 0])
        text = cache.story_text(conn, rows)
        stories = [cache.merged(s, text[s.id]) for s in rows if s.id in text]
        pairs, suspects = find_pairs(stories, vecs, thr)
        by_id = {s.id: s for s in stories}
        moved: dict[int, int] = {}

        def root(i: int) -> int:
            while i in moved:
                i = moved[i]
            return i

        if pairs:
            source_type = dict(conn.execute(select(db.sources.c.id, db.sources.c.source_type)).all())
            primary = lambda m: m.domain in config.PRIMARY_DOMAINS or source_type.get(m.source_id) == "primary"  # noqa: E731
        for sim, reason, a_id, b_id in pairs:
            if stats["merged_stories"] >= max_merges:
                break
            a, b = by_id[root(a_id)], by_id[root(b_id)]
            if a.id == b.id:
                continue
            keep, drop = sorted((a, b), key=lambda s: (db.as_utc(s.first_published_at) or now, s.id))
            members = sorted((m for m in cache.articles(conn).values()
                              if m.story_id in (keep.id, drop.id) and m.status in ("published", "overflow")), key=lambda m: m.id)
            if len(members) > config.CLUSTER_MAX_TOTAL:
                # A merge used to move every member over whatever the size: chained merges grew one
                # story to 161 sources. Two stories that would pass the ceiling together stay two
                # (the cluster step's repair keeps the shown part of a merged story at its cap).
                stats["too_big"] = stats.get("too_big", 0) + 1
                continue
            texts = cache.article_text(conn, [m for m in members if m.status == "published"])
            done = merge_stories(conn, keep, drop, members, texts, primary, vecs.get(keep.id), vecs.get(drop.id), now)
            moved[drop.id] = keep.id
            keep.article_count = len(members)
            keep.thread_id = done.get("thread_id", keep.thread_id)
            if done.get("headline"):
                keep.headline = done["headline"]
            stats["merged_stories"] += 1
            stats["articles_moved"] += drop.article_count or 1
            log.info("story #%s (%s) merged into #%s: %s", drop.id, drop.headline, keep.id, reason)
        live = [(sim, reason, a, b) for sim, reason, a, b in suspects if a not in moved and b not in moved][:MAX_SUSPECTS]
        cache.SUSPECTS.put(conn, {"pairs": [{"a": a, "b": b, "sim": round(sim, 3), "reason": reason} for sim, reason, a, b in live]})
        stats["suspects"] = len(live)
    return stats
