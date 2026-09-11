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
THRESHOLD_ANY = 0.78
THRESHOLD_SPECIFIC = 0.72
# Entities present in more than this share of recent stories (Anthropic, OpenAI, Claude...) say
# nothing about which saga a story belongs to, so they do not count as a shared entity.
UBIQUITOUS_SHARE = 0.15


def _names(entities: dict | None) -> set[str]:
    out = set()
    for kind in ("companies", "models", "people"):
        for n in (entities or {}).get(kind, []) or []:
            n = str(n).strip().lower()
            if len(n) > 2:
                out.add(n)
    return out


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
            return stats
        threads = conn.execute(select(db.threads).where(db.threads.c.updated_at >= since)).all()
        vecs = {t.id: np.asarray(t.embedding, dtype=np.float32) for t in threads}
        meta = {t.id: {"names": _names(t.entities), "count": t.story_count, "ents": t.entities or {}} for t in threads}

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
                shared_any = names_all & meta[tid]["names"]
                if not shared_any:
                    continue
                sim = float(np.dot(v, tv))
                specific = bool(names & (meta[tid]["names"] - ubiquitous))
                if (sim >= THRESHOLD_ANY or (specific and sim >= THRESHOLD_SPECIFIC)) and sim > best_sim:
                    best, best_sim = tid, sim
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
    return stats
