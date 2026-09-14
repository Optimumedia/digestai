"""Step 5: embed articles and group them into stories."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta

import numpy as np
from sqlalchemy import insert, select, update

from . import config, db
from .textutil import short_hash, slugify, tokens

log = logging.getLogger("digest.cluster")

_MODEL = None
_MODEL_NAME = "fallback-hash"


def _load_model():
    global _MODEL, _MODEL_NAME
    if _MODEL is not None:
        return _MODEL
    try:
        from fastembed import TextEmbedding

        _MODEL = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", cache_dir=str(config.ROOT / ".fastembed_cache"))
        _MODEL_NAME = "bge-small-en-v1.5"
    except Exception as exc:  # noqa: BLE001
        log.warning("fastembed unavailable (%s); using hashed bag-of-words embeddings", exc)
        _MODEL = False
    return _MODEL


def _hash_embed(text: str, dim: int = 512) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    toks = tokens(text)
    grams = toks + [" ".join(toks[i : i + 2]) for i in range(len(toks) - 1)]
    for g in grams:
        h = int(hashlib.md5(g.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0 if (h >> 20) & 1 else -1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm else vec


def embed(texts: list[str]) -> list[list[float]]:
    model = _load_model()
    if model:
        out = []
        for v in model.embed(texts, batch_size=16):
            v = np.asarray(v, dtype=np.float32)
            n = np.linalg.norm(v)
            out.append((v / n if n else v).tolist())
        return out
    return [_hash_embed(t).tolist() for t in texts]


def threshold() -> float:
    _load_model()
    return config.CLUSTER_THRESHOLD_MODEL if _MODEL_NAME != "fallback-hash" else config.CLUSTER_THRESHOLD_FALLBACK


def _story_text(row) -> str:
    kp = " ".join(row.key_points or []) if isinstance(row.key_points, list) else ""
    return f"{row.headline or row.title}. {kp} {(row.summary_md or '')[:800]}"


def _unique_slug(conn, base: str, table, url_hint: str) -> str:
    slug = base
    if conn.execute(select(table.c.id).where(table.c.slug == slug)).first():
        slug = f"{base[:70]}-{short_hash(url_hint)}"
    return slug


def run() -> dict:
    stats = {"embedded": 0, "new_stories": 0, "merged": 0, "model": None}
    eng = db.engine()
    with eng.connect() as conn:
        rows = conn.execute(select(db.articles).where(db.articles.c.status == "enriched")).all()
    if not rows:
        return stats

    vectors = embed([_story_text(r) for r in rows])
    stats["model"] = _MODEL_NAME
    stats["embedded"] = len(vectors)
    thr = threshold()
    since = db.utcnow() - timedelta(hours=config.CLUSTER_WINDOW_HOURS)

    with eng.begin() as conn:
        recent = conn.execute(
            # Candidates are stories that broke inside the window. Filtering on updated_at let a
            # popular story absorb new articles forever, because every merge refreshed it.
            select(db.stories).where(db.stories.c.first_published_at >= since, db.stories.c.embedding.isnot(None))
        ).all()
        story_vecs = {s.id: np.asarray(s.embedding, dtype=np.float32) for s in recent}
        story_meta = {s.id: {"count": s.article_count, "importance": s.importance, "lead": s.lead_article_id} for s in recent}

        for row, vec in zip(rows, vectors):
            v = np.asarray(vec, dtype=np.float32)
            best_id, best_sim = None, 0.0
            for sid, sv in story_vecs.items():
                sim = float(np.dot(v, sv))
                if sim > best_sim:
                    best_id, best_sim = sid, sim
            now = db.utcnow()
            base_slug = slugify(row.headline or row.title)
            article_slug = _unique_slug(conn, base_slug, db.articles, row.url)

            if best_id is not None and best_sim >= thr:
                meta = story_meta[best_id]
                n = meta["count"]
                merged = (story_vecs[best_id] * n + v) / (n + 1)
                merged /= np.linalg.norm(merged) or 1.0
                story_vecs[best_id] = merged
                meta["count"] = n + 1
                values = {"article_count": n + 1, "updated_at": now, "embedding": merged.tolist()}
                if (row.importance or 0) > (meta["importance"] or 0):
                    # A more important article becomes the lead: its digest describes the story.
                    meta["importance"], meta["lead"] = row.importance, row.id
                    values.update(headline=row.headline, summary_md=row.summary_md, key_points=row.key_points,
                                  why_it_matters=row.why_it_matters, category=row.category, entities=row.entities,
                                  importance=row.importance, lead_article_id=row.id)
                conn.execute(update(db.stories).where(db.stories.c.id == best_id).values(**values))
                story_id = best_id
                stats["merged"] += 1
            else:
                story_slug = _unique_slug(conn, base_slug, db.stories, row.url)
                res = conn.execute(insert(db.stories).values(
                    slug=story_slug,
                    headline=row.headline or row.title,
                    summary_md=row.summary_md,
                    key_points=row.key_points,
                    why_it_matters=row.why_it_matters,
                    category=row.category,
                    entities=row.entities,
                    lead_article_id=row.id,
                    article_count=1,
                    importance=row.importance or 5,
                    score=0.0,
                    embedding=v.tolist(),
                    status="published",
                    first_published_at=db.as_utc(row.published_at) or now,
                    updated_at=now,
                ))
                story_id = res.inserted_primary_key[0]
                story_vecs[story_id] = v
                story_meta[story_id] = {"count": 1, "importance": row.importance or 5, "lead": row.id}
                stats["new_stories"] += 1

            conn.execute(update(db.articles).where(db.articles.c.id == row.id).values(
                story_id=story_id, slug=article_slug, embedding=vec, status="published",
            ))
    return stats
