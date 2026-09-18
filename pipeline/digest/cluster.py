"""Step 5: embed articles and group them into stories."""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

import numpy as np
from sqlalchemy import insert, select, update

from . import cache, config, db, merge
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
    """A free address. Both the plain one and the one with the article's hash can be taken: a merged
    story keeps its address, and its articles come back through clustering with the same headline and
    the same link, so the hashed address is already in use too."""
    base = (base or "story")[:70].strip("-") or "story"
    taken = {r[0] for r in conn.execute(
        select(table.c.slug).where(table.c.slug.like(f"{base}%"))).all()}
    if base not in taken:
        return base
    stem = f"{base}-{short_hash(url_hint)}"
    if stem not in taken:
        return stem
    for n in range(2, 60):
        if f"{stem}-{n}" not in taken:
            return f"{stem}-{n}"
    return f"{stem}-{short_hash(url_hint + db.utcnow().isoformat())}"


def _vec(value) -> np.ndarray | None:
    return db.unpack_vec(value)  # a packed string or a JSON list (db.pack_vec)


# ------------------------------------------------------------------ merge rule

def pick_story(v: np.ndarray, stories: dict[int, dict], thr: float, lead_thr: float) -> tuple[int | None, float]:
    """The story an article joins, or None for a new story.

    A story's embedding is the running mean of its shown members. As a story grows that mean turns
    into a generic "about Mistral" vector that attracts everything on the topic (one story had
    446 sources), so an article must also be close to the story's lead article. A full story
    (CLUSTER_MAX_ARTICLES shown) still matches: the caller attaches the article as overflow, which
    counts as coverage but is not shown, rather than starting a second story for the same event.
    `stories` maps id -> {"vec": mean, "lead_vec": lead embedding or None, ...}.
    """
    best_id, best_sim = None, -1.0
    for sid, s in stories.items():
        sv = s["vec"]
        if sv is None or sv.shape != v.shape:
            continue
        sim_mean = float(np.dot(v, sv))
        if sim_mean < thr:
            continue
        lead = s.get("lead_vec")
        if lead is not None and lead.shape == v.shape:
            sim_lead = float(np.dot(v, lead))
            if sim_lead < lead_thr:
                continue
        else:
            sim_lead = sim_mean
        score = min(sim_mean, sim_lead)
        if score > best_sim:
            best_id, best_sim = sid, score
    return best_id, best_sim


def split_members(lead_id: int, lead_vec: np.ndarray, members: list[tuple[int, np.ndarray | None]],
                  lead_thr: float, cap: int) -> tuple[list[int], list[int], list[int]]:
    """Repair of an oversized story: the lead and the members closest to it (at most `cap`) stay
    shown; close members beyond the cap become overflow (counted, not shown); members far from the
    lead are detached, least similar first, and clustered again. Members without a comparable
    embedding rank after the close ones."""
    judged, unjudged = [], []
    for aid, vec in members:
        if aid == lead_id:
            continue
        if vec is None or vec.shape != lead_vec.shape:
            unjudged.append(aid)
        else:
            judged.append((float(np.dot(vec, lead_vec)), aid))
    judged.sort(key=lambda x: -x[0])
    close = [aid for sim, aid in judged if sim >= lead_thr]
    far = [aid for sim, aid in judged if sim < lead_thr]
    ranked = [lead_id] + close + unjudged
    # Least similar first, so a bounded run detaches the worst matches before the borderline ones.
    return ranked[:cap], ranked[cap:], list(reversed(far))


def _mean_vec(vecs: list[np.ndarray]) -> np.ndarray | None:
    if not vecs:
        return None
    m = np.mean(np.stack(vecs), axis=0)
    n = np.linalg.norm(m)
    return m / n if n else m


def lead_threshold(thr: float) -> float:
    return thr - config.CLUSTER_LEAD_MARGIN


def merge_threshold() -> float:
    _load_model()
    return config.MERGE_THRESHOLD_MODEL if _MODEL_NAME != "fallback-hash" else config.MERGE_THRESHOLD_FALLBACK


def _is_primary(row, source_type: dict) -> bool:
    return row is not None and (row.domain in config.PRIMARY_DOMAINS or source_type.get(row.source_id) == "primary")


def repair_oversized(eng, lead_thr: float, cap: int = None, max_detach: int = None) -> dict:
    """Stories showing more members than the cap (grown under the old merge rule, or two stories
    merged into one): members far from the lead go back to status 'enriched' (embedding kept) and
    are clustered again in this run; close members beyond the cap become overflow. Members and
    their embeddings come from the runner's copy (cache.py), so a run with nothing to repair
    reads nothing."""
    cap = cap or config.CLUSTER_MAX_ARTICLES
    max_detach = config.CLUSTER_REPAIR_MAX_PER_RUN if max_detach is None else max_detach
    stats = {"stories_repaired": 0, "articles_detached": 0, "articles_overflow": 0}
    with eng.begin() as conn:
        members_of: dict[int, list] = {}
        for a in cache.articles(conn).values():
            if a.story_id and a.status in ("published", "overflow"):
                members_of.setdefault(a.story_id, []).append(a)
        shown_n = {sid: sum(1 for m in ms if m.status == "published") for sid, ms in members_of.items()}
        big = sorted((sid for sid, n in shown_n.items() if n > cap), key=lambda sid: (-shown_n[sid], sid))
        if not big:
            return stats
        stories = cache.stories(conn)
        for sid in big:
            s = stories.get(sid)
            if s is None or s.status != "published":
                continue
            budget = max_detach - stats["articles_detached"] - stats["articles_overflow"]
            if budget <= 0:
                break
            shown = sorted((m for m in members_of[sid] if m.status == "published"), key=lambda m: m.id)
            vecs = cache.article_vectors(conn, shown)
            lead_id = s.lead_article_id if vecs.get(s.lead_article_id) is not None else None
            if lead_id is None:
                with_vec = [m for m in shown if vecs.get(m.id) is not None]
                if not with_vec:
                    continue
                lead_id = max(with_vec, key=lambda m: (m.importance or 0, -m.id)).id
            keep, overflow, detach = split_members(lead_id, vecs[lead_id], [(m.id, vecs.get(m.id)) for m in shown], lead_thr, cap)
            detach = detach[:budget]
            overflow = overflow[:budget - len(detach)]
            if detach:
                conn.execute(update(db.articles).where(db.articles.c.id.in_(detach))
                             .values(story_id=None, status="enriched"))
            if overflow:
                conn.execute(update(db.articles).where(db.articles.c.id.in_(overflow)).values(status="overflow"))
            values = {"article_count": len(members_of[sid]) - len(detach)}
            mean = _mean_vec([vecs[a] for a in keep if vecs.get(a) is not None and vecs[a].shape == vecs[lead_id].shape])
            if mean is not None:
                values["embedding"] = db.pack_vec(mean)
            conn.execute(update(db.stories).where(db.stories.c.id == sid).values(**values))
            stats["stories_repaired"] += 1
            stats["articles_detached"] += len(detach)
            stats["articles_overflow"] += len(overflow)
            log.info("story #%s: %d shown, detached %d far from the lead, %d beyond the cap kept as overflow",
                     sid, len(shown), len(detach), len(overflow))
    return stats


def run() -> dict:
    stats = {"embedded": 0, "new_stories": 0, "merged": 0, "overflow": 0, "model": None}
    eng = db.engine()
    thr = threshold()
    lead_thr = lead_threshold(thr)
    cap = config.CLUSTER_MAX_ARTICLES
    # Two published stories for one event become one (the older keeps its address) before new
    # articles are placed, so they join the surviving story.
    duplicates = merge.run(eng, merge_threshold())
    if duplicates["merged_stories"] or duplicates["suspects"]:
        stats["duplicates"] = duplicates
    repair = repair_oversized(eng, lead_thr, cap)
    if repair["stories_repaired"]:
        stats.update(repair)

    a = db.articles.c
    with eng.connect() as conn:
        rows = conn.execute(
            # The columns used below: article text is not needed to cluster (it is read by the database
            # meter as megabytes per run when selected with the rest of the row).
            select(a.id, a.url, a.slug, a.title, a.headline, a.summary_md, a.key_points, a.why_it_matters, a.category,
                   a.entities, a.importance, a.embedding, a.published_at, a.content_type, a.domain, a.source_id)
            .where(a.status == "enriched")
            .order_by(a.published_at.asc(), a.id.asc())
        ).all()
    if not rows:
        return stats

    # Articles detached by a repair keep their embedding; only new ones are embedded.
    vectors: list = [_vec(r.embedding) for r in rows]
    need = [i for i, v in enumerate(vectors) if v is None]
    if need:
        for i, vec in zip(need, embed([_story_text(rows[i]) for i in need])):
            vectors[i] = vec
    stats["model"] = _MODEL_NAME
    stats["embedded"] = len(need)
    since = db.utcnow() - timedelta(hours=config.CLUSTER_WINDOW_HOURS)

    with eng.begin() as conn:
        # Candidates are stories that broke inside the window. Filtering on updated_at let a
        # popular story absorb new articles forever, because every merge refreshed it. Their rows and
        # vectors come from the runner's copy (cache.py); only stories changed since the last run are read.
        recent = sorted((s for s in cache.stories(conn).values()
                         if s.status == "published" and s.len_embedding >= 0
                         and (db.as_utc(s.first_published_at) or since) >= since), key=lambda s: s.id)
        story_vecs = cache.story_vectors(conn, recent)
        recent = [s for s in recent if story_vecs.get(s.id) is not None]
        mirror = cache.articles(conn)
        shown: dict[int, int] = {}
        for m in mirror.values():
            if m.status == "published" and m.story_id:
                shown[m.story_id] = shown.get(m.story_id, 0) + 1
        lead_rows = [mirror[s.lead_article_id] for s in recent if s.lead_article_id in mirror]
        lead_vecs = cache.article_vectors(conn, lead_rows)
        # A lead outside the copy's window (rare): read its embedding directly.
        other = [s.lead_article_id for s in recent if s.lead_article_id and s.lead_article_id not in mirror]
        for i in range(0, len(other), 500):
            for r in conn.execute(select(a.id, a.embedding).where(a.id.in_(other[i : i + 500]))).all():
                lead_vecs[r.id] = _vec(r.embedding)
        # The story's headline is its lead's; with the lead's type and source it decides whether a
        # newcomer may take over (merge.better_lead). Both come from the runner's copy.
        story_text = cache.story_text(conn, recent)
        source_type = dict(conn.execute(select(db.sources.c.id, db.sources.c.source_type)).all())
        candidates = {s.id: {"vec": story_vecs[s.id], "lead_vec": lead_vecs.get(s.lead_article_id),
                             "count": s.article_count or 1, "shown": shown.get(s.id, 1)}
                      for s in recent}
        story_meta = {}
        for s in recent:
            lead = mirror.get(s.lead_article_id)
            story_meta[s.id] = {"importance": s.importance, "lead": s.lead_article_id,
                                "headline": story_text[s.id].headline if s.id in story_text else None,
                                "content_type": lead.content_type if lead is not None else None,
                                "primary": _is_primary(lead, source_type)}

        for row, vec in zip(rows, vectors):
            v = np.asarray(vec, dtype=np.float32)
            best_id, _sim = pick_story(v, candidates, thr, lead_thr)
            now = db.utcnow()
            base_slug = slugify(row.headline or row.title)
            # A re-clustered article keeps its slug (links to it must not change).
            article_slug = row.slug or _unique_slug(conn, base_slug, db.articles, row.url)
            status = "published"

            if best_id is not None:
                meta, cand = story_meta[best_id], candidates[best_id]
                n = cand["count"]
                cand["count"] = n + 1
                values = {"article_count": n + 1, "updated_at": now}
                if cand["shown"] >= cap:
                    # The story is full: one more source counts as coverage, but the page lists no more
                    # and the story's mean and lead stay put. Never a second story for the same event.
                    status = "overflow"
                    stats["overflow"] += 1
                else:
                    merged = (cand["vec"] * cand["shown"] + v) / (cand["shown"] + 1)
                    merged /= np.linalg.norm(merged) or 1.0
                    cand["vec"], cand["shown"] = merged, cand["shown"] + 1
                    values["embedding"] = db.pack_vec(merged)
                    new = {"headline": row.headline or row.title, "content_type": row.content_type,
                           "importance": row.importance, "primary": _is_primary(row, source_type)}
                    if merge.better_lead(new, meta):
                        # The new lead's digest describes the story: news over commentary, primary
                        # sources first, and only a clearly more important article changes the headline.
                        meta.update(importance=row.importance, lead=row.id, headline=new["headline"],
                                    content_type=row.content_type, primary=new["primary"])
                        cand["lead_vec"] = v
                        values.update(headline=new["headline"], summary_md=row.summary_md, key_points=row.key_points,
                                      why_it_matters=row.why_it_matters, category=row.category, entities=row.entities,
                                      importance=row.importance, lead_article_id=row.id)
                    stats["merged"] += 1
                conn.execute(update(db.stories).where(db.stories.c.id == best_id).values(**values))
                story_id = best_id
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
                    embedding=db.pack_vec(v),
                    status="published",
                    first_published_at=db.as_utc(row.published_at) or now,
                    updated_at=now,
                ))
                story_id = res.inserted_primary_key[0]
                candidates[story_id] = {"vec": v, "lead_vec": v, "count": 1, "shown": 1}
                story_meta[story_id] = {"importance": row.importance or 5, "lead": row.id, "headline": row.headline or row.title,
                                        "content_type": row.content_type, "primary": _is_primary(row, source_type)}
                stats["new_stories"] += 1

            conn.execute(update(db.articles).where(db.articles.c.id == row.id).values(
                story_id=story_id, slug=article_slug, embedding=db.pack_vec(v), status=status,
            ))
    return stats
