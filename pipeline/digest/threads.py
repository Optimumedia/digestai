"""Step: group stories into developing threads ("the story so far").

A thread is a run of stories over days about the same saga: an exodus at a lab, a lawsuit,
a model family's rollout. Stories join a thread when they are close in meaning to the
thread's centre AND share at least one named entity, within a rolling window.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
from sqlalchemy import insert, select, update

from . import config, db
from .textutil import short_hash, slugify

log = logging.getLogger("digest.threads")

WINDOW_DAYS = 14
# Measured on bge-small embeddings: episodes of one saga score ~0.80+, unrelated stories about
# the same company 0.65-0.70. Two ways in: very close with any shared name, or fairly close
# with a shared name that is specific (not one of the names in every other story).
THRESHOLD_ANY = 0.88
THRESHOLD_SPECIFIC = 0.78
MAX_EPISODES = 10  # past this a "thread" is a topic; the next episode starts a fresh thread
# Entities present in more than this share of recent stories (Anthropic, OpenAI, Claude...) say
# nothing about which saga a story belongs to, so they do not count as a shared entity.
UBIQUITOUS_SHARE = 0.06


def _names(entities: dict | None) -> set[str]:
    out = set()
    for kind in ("companies", "models", "people"):
        for n in (entities or {}).get(kind, []) or []:
            n = str(n).strip().lower()
            if len(n) > 2:
                out.add(n)
    return out


NAME_PROMPT = """These headlines are episodes of one developing news story about artificial intelligence, oldest first:

{episodes}

Return ONLY JSON: {{"title": "<a 5-10 word name for the whole saga, like a newspaper series title, no colon, no date>",
"summary": "<two sentences: what the saga is about and where it stands after the latest episode>"}}"""

NAME_AT = (2, 4, 7, 12)  # story counts at which a thread gets (re)named
MAX_NAMES_PER_RUN = 4


def name_threads(eng) -> int:
    """Give multi-episode threads a proper name and summary with the LLM, budgeted."""
    from . import enrich

    if not (config.GROQ_API_KEY or config.GEMINI_API_KEY):
        return 0
    named = 0
    with eng.connect() as conn:
        candidates = conn.execute(
            select(db.threads).where(db.threads.c.story_count >= 2, db.threads.c.status == "published")
            .order_by(db.threads.c.updated_at.desc()).limit(40)
        ).all()
        allowance = enrich.allowance(conn, "groq") if config.GROQ_API_KEY else enrich.allowance(conn, "gemini")
    budget = min(MAX_NAMES_PER_RUN, allowance)
    for t in candidates:
        if named >= budget:
            break
        due = any(t.story_count >= n > t.named_count for n in NAME_AT)
        if not due:
            continue
        with eng.connect() as conn:
            eps = conn.execute(
                select(db.stories.c.headline, db.stories.c.first_published_at)
                .where(db.stories.c.thread_id == t.id, db.stories.c.status == "published")
                .order_by(db.stories.c.first_published_at.asc())
            ).all()
        prompt = NAME_PROMPT.format(episodes="\n".join(f"- {e.headline}" for e in eps))
        try:
            if config.GROQ_API_KEY:
                result = enrich.call_groq(prompt)
                enrich.record_usage(eng, "groq", 1)
            else:
                result = enrich.call_gemini(prompt)
                enrich.record_usage(eng, "gemini", 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("thread naming failed for %s: %s", t.slug, exc)
            continue
        title = str(result.get("title") or "").strip().rstrip(".")[:120]
        summary = str(result.get("summary") or "").strip()[:600]
        if len(title) < 8:
            continue
        with eng.begin() as conn:
            conn.execute(update(db.threads).where(db.threads.c.id == t.id)
                         .values(title=title, summary=summary or t.summary, named_count=t.story_count))
        named += 1
    return named


def run() -> dict:
    stats = {"assigned": 0, "new_threads": 0}
    eng = db.engine()
    now = db.utcnow()
    since = now - timedelta(days=WINDOW_DAYS)
    with eng.begin() as conn:
        stories = conn.execute(
            select(db.stories).where(db.stories.c.thread_id.is_(None), db.stories.c.status == "published",
                                     db.stories.c.embedding.isnot(None))
            .order_by(db.stories.c.first_published_at.asc())
        ).all()
        if not stories:
            stats["named"] = name_threads(eng)
            return stats
        threads = conn.execute(select(db.threads).where(db.threads.c.updated_at >= since, db.threads.c.story_count < MAX_EPISODES)).all()
        vecs = {t.id: np.asarray(t.embedding, dtype=np.float32) for t in threads}
        meta = {t.id: {"names": _names(t.entities), "count": t.story_count, "ents": t.entities or {}} for t in threads}
        # The latest episode's own vector: a thread's centroid drifts as it grows, so the "very
        # close" test is made against the most recent episode rather than the average.
        latest_vec: dict[int, np.ndarray] = {}
        for t in threads:
            row = conn.execute(select(db.stories.c.embedding).where(db.stories.c.thread_id == t.id, db.stories.c.embedding.isnot(None))
                               .order_by(db.stories.c.first_published_at.desc()).limit(1)).first()
            if row:
                latest_vec[t.id] = np.asarray(row.embedding, dtype=np.float32)

        # Entity frequency over the window decides which names are too common to be a signal.
        recent = conn.execute(select(db.stories.c.entities).where(db.stories.c.first_published_at >= since)).all()
        freq: dict[str, int] = {}
        for r in recent:
            for n in _names(r.entities):
                freq[n] = freq.get(n, 0) + 1
        total = max(1, len(recent))
        ubiquitous = {n for n, c in freq.items() if c >= 3 and c / total > UBIQUITOUS_SHARE}

        for s in stories:
            v = np.asarray(s.embedding, dtype=np.float32)
            names_all = _names(s.entities)
            names = names_all - ubiquitous
            best, best_sim = None, 0.0
            for tid, tv in vecs.items():
                if meta[tid]["count"] >= MAX_EPISODES:
                    continue
                shared_any = names_all & meta[tid]["names"]
                if not shared_any:
                    continue
                sim = float(np.dot(v, tv))
                sim_latest = float(np.dot(v, latest_vec[tid])) if tid in latest_vec else sim
                specific = bool(names & (meta[tid]["names"] - ubiquitous))
                if (sim_latest >= THRESHOLD_ANY or (specific and sim_latest >= THRESHOLD_SPECIFIC)) and sim_latest > best_sim:
                    best, best_sim = tid, sim_latest
            if best is not None:
                n = meta[best]["count"]
                merged = (vecs[best] * n + v) / (n + 1)
                merged /= np.linalg.norm(merged) or 1.0
                vecs[best] = merged
                meta[best]["count"] = n + 1
                meta[best]["names"] |= names_all
                ents = meta[best]["ents"]
                for kind in ("companies", "models", "people"):
                    have = [x.lower() for x in ents.get(kind, [])]
                    for x in (s.entities or {}).get(kind, []) or []:
                        if x.lower() not in have:
                            ents.setdefault(kind, []).append(x)
                conn.execute(update(db.threads).where(db.threads.c.id == best).values(
                    story_count=n + 1, updated_at=now, embedding=merged.tolist(), entities=ents,
                ))
                conn.execute(update(db.stories).where(db.stories.c.id == s.id).values(thread_id=best))
                stats["assigned"] += 1
            else:
                base = slugify(s.headline)
                slug = base
                if conn.execute(select(db.threads.c.id).where(db.threads.c.slug == slug)).first():
                    slug = f"{base[:70]}-{short_hash(str(s.id))}"
                res = conn.execute(insert(db.threads).values(
                    slug=slug, title=s.headline, summary=s.why_it_matters, category=s.category,
                    entities=s.entities or {}, embedding=v.tolist(), story_count=1,
                    first_at=db.as_utc(s.first_published_at) or now, updated_at=now, status="published",
                ))
                tid = res.inserted_primary_key[0]
                vecs[tid] = v
                meta[tid] = {"names": set(names_all), "count": 1, "ents": dict(s.entities or {})}
                conn.execute(update(db.stories).where(db.stories.c.id == s.id).values(thread_id=tid))
                stats["new_threads"] += 1
    stats["named"] = name_threads(eng)
    return stats
