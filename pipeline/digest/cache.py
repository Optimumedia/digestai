"""The runner's copy of what earlier runs read, so a run asks the database only for what changed.

Supabase's free plan meters every byte the database sends (5 GB a month), and most of what a run
needs did not change since the previous run: story and article rows, summaries, article texts,
embeddings. A copy lives in pipeline/data/cache (kept between runs by the Actions cache):

- Mirror: the small columns of recent stories and articles. The database stamps every insert and
  update with a revision and records deletions (db.install_change_tracking), so each run reads
  only rows written since the watermark of the previous read. A missing, foreign or week-old copy,
  or a database without the triggers, is replaced by one full read of the window.
- Details: large columns (summaries, key points, article text), read by id and kept while the
  row's fingerprint (lengths of those columns, from the mirror) is unchanged, re-read at least
  monthly in case something was edited by hand without changing a length.
- Vectors: embeddings as float16, kept while their fingerprint is unchanged (embeddings are
  written once; a story's changes with each merge, which its fingerprint follows).

Everything here is an optimisation: deleting the directory costs one full read, never a wrong result.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import time
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from sqlalchemy import Text, and_, case, cast, func, or_, select

from . import config, db

log = logging.getLogger("digest.cache")

FORMAT = 1
BATCH = 500
FULL_REFRESH_DAYS = 7       # the mirror is re-read in full at least this often
DETAIL_MAX_AGE_DAYS = 30    # details are re-read at least this often (spread over three days by id)
UNUSED_DAYS = 3             # details and vectors nobody asked for in this long are dropped
RECENT_ARTICLE_DAYS = 16    # articles of any status kept this long (admin looks back 14 days)
URL_DAYS = 30               # known article addresses kept this long


def horizon_days() -> int:
    """Published articles and stories are kept as long as the export can show them."""
    return config.EXPORT_DAYS + 10


def db_key(conn) -> str:
    return hashlib.sha256(conn.engine.url.render_as_string(hide_password=False).encode()).hexdigest()[:16]


def _enc(v):
    return {"$d": v.isoformat()} if isinstance(v, datetime) else v


def _dec(v):
    if isinstance(v, dict) and len(v) == 1 and "$d" in v:
        return datetime.fromisoformat(v["$d"])
    return v


def iso(dt) -> str | None:
    dt = db.as_utc(dt)
    return dt.isoformat() if dt else None


def _load_json(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return None
    return data if isinstance(data, dict) and data.get("format") == FORMAT else None


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=3) as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    os.replace(tmp, path)


class _Store:
    """One cache file per database; loaded on first use, written by save_all()."""

    registry: list["_Store"] = []
    suffix = ".json.gz"

    def __init__(self, name: str):
        self.name = name
        self.states: dict[str, dict] = {}
        _Store.registry.append(self)

    def _path(self, key: str) -> Path:
        return config.CACHE_DIR / key / (self.name + self.suffix)

    def state(self, conn) -> dict:
        key = db_key(conn)
        if key not in self.states:
            path = self._path(key)
            st = self.load(path) or self.empty()
            st["_path"], st["_dirty"] = str(path), False
            self.states[key] = st
        return self.states[key]

    def load(self, path: Path) -> dict | None:
        data = _load_json(path)
        return self.decode(data) if data else None

    def save(self) -> None:
        for st in self.states.values():
            if st.get("_dirty"):
                try:
                    self.write(Path(st["_path"]), st)
                    st["_dirty"] = False
                except OSError as exc:
                    log.warning("could not save the %s cache: %s", self.name, exc)

    def write(self, path: Path, st: dict) -> None:
        _save_json(path, {"format": FORMAT, **self.encode(st)})

    def empty(self) -> dict:
        return {}

    def decode(self, data: dict) -> dict:
        return data

    def encode(self, st: dict) -> dict:
        return {k: v for k, v in st.items() if not k.startswith("_")}


def save_all() -> None:
    for store in _Store.registry:
        store.save()


def reset() -> None:
    """Forget what is loaded in memory (tests)."""
    for store in _Store.registry:
        store.states.clear()


def forget(conn) -> None:
    """Drop everything kept for this database, in memory and on disk."""
    key = db_key(conn)
    for store in _Store.registry:
        store.states.pop(key, None)
        try:
            store._path(key).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------- mirror

class Mirror(_Store):
    def __init__(self, name: str, table, columns: list, scope, keep):
        super().__init__(name)
        self.table, self.columns, self.scope, self.keep = table, columns, scope, keep
        self.names = [c.key if getattr(c, "key", None) else c.name for c in columns]
        self.Row = namedtuple(f"{name.title()}Row", self.names)

    def empty(self) -> dict:
        return {"wm": None, "full_at": 0.0, "cols": self.names, "rows": {}}

    def decode(self, data: dict) -> dict | None:
        if data.get("cols") != self.names:
            return None
        rows = {int(r[0]): self.Row(*[_dec(v) for v in r]) for r in data.get("rows") or []}
        return {"wm": data.get("wm"), "full_at": float(data.get("full_at") or 0), "cols": self.names, "rows": rows}

    def encode(self, st: dict) -> dict:
        return {"wm": st["wm"], "full_at": st["full_at"], "cols": self.names,
                "rows": [[_enc(v) for v in r] for r in st["rows"].values()]}

    def rows(self, conn) -> dict[int, tuple]:
        """Rows in the window, as they are in the database now."""
        st = self.state(conn)
        now = db.utcnow()
        rows: dict[int, tuple] = st["rows"]
        if not db.change_tracking_ready(conn):
            rows = {r[0]: self.Row(*r) for r in conn.execute(select(*self.columns).where(self.scope(now))).all()}
            st.update(wm=None, rows=rows)
            return dict(rows)
        wm = db.watermark(conn)
        stale = time.time() - st["full_at"] > FULL_REFRESH_DAYS * 86400
        if st["wm"] is not None and wm < st["wm"]:
            # The database went back in time (restored from a backup): nothing kept for it can be trusted.
            log.warning("database revision went backwards; forgetting the cached copy")
            forget(conn)
            st = self.state(conn)
            rows = st["rows"]
        if st["wm"] is None or stale:
            rows = {r[0]: self.Row(*r) for r in conn.execute(select(*self.columns).where(self.scope(now))).all()}
            st["full_at"] = time.time()
        else:
            for r in conn.execute(select(*self.columns).where(self.table.c.rev >= st["wm"])).all():
                rows[r[0]] = self.Row(*r)
            for (rid,) in conn.execute(select(db.deleted_rows.c.row_id).where(
                    db.deleted_rows.c.table_name == self.table.name, db.deleted_rows.c.rev >= st["wm"])).all():
                rows.pop(rid, None)
        rows = {k: r for k, r in rows.items() if self.keep(r, now)}
        st.update(wm=wm, rows=rows, _dirty=True)
        return dict(rows)


def _len(col):
    return func.coalesce(func.length(cast(col, Text)), -1)


def _sig(*cols):
    """One number that changes when the length of any of these columns does."""
    expr = None
    for i, c in enumerate(cols, 1):
        term = _len(c) * i
        expr = term if expr is None else expr + term
    return expr


_a, _s, _t = db.articles.c, db.stories.c, db.threads.c

ARTICLE_TEXT_COLUMNS = [_a.slug, _a.url, _a.title, _a.raw_title, _a.headline, _a.author, _a.fetched_at, _a.description,
                        _a.image_url, _a.summary_md, _a.key_points, _a.why_it_matters, _a.entities, _a.model_release,
                        _a.funding, _a.work_card, _a.discussion_url]
STORY_TEXT_COLUMNS = [_s.slug, _s.headline, _s.summary_md, _s.key_points, _s.why_it_matters, _s.entities, _s.pulse]

ARTICLES = Mirror(
    "articles", db.articles,
    [_a.id, _a.story_id, _a.source_id, _a.status, _a.created_at, _a.published_at, _a.importance, _a.content_type,
     _a.category, _a.domain, _a.simhash, _a.discussion_site, _a.discussion_points, _a.trend_score, _a.engagement,
     _a.predicted_score, _a.word_count, _a.show_fulltext, _a.extraction_method, _a.enrich_model,
     func.substr(_a.reject_reason, 1, 8).label("reject_kind"),
     case((func.coalesce(func.length(_a.discussion_url), 0) > 0, 1), else_=0).label("has_discussion_url"),
     _len(_a.content_md).label("len_content_md"), _len(_a.embedding).label("len_embedding"),
     _sig(*[c for c in ARTICLE_TEXT_COLUMNS if c.name != "fetched_at"]).label("sig")],
    # "overflow": a member of a full story, counted as coverage but not shown (cluster.py).
    scope=lambda now: or_(_a.created_at >= now - timedelta(days=RECENT_ARTICLE_DAYS),
                          and_(_a.status.in_(["published", "overflow"]), _a.created_at >= now - timedelta(days=horizon_days()))),
    keep=lambda r, now: (db.as_utc(r.created_at) or now) >= now - timedelta(days=RECENT_ARTICLE_DAYS)
    or (r.status in ("published", "overflow") and (db.as_utc(r.created_at) or now) >= now - timedelta(days=horizon_days())),
)

STORIES = Mirror(
    "stories", db.stories,
    [_s.id, _s.status, _s.score, _s.pinned, _s.thread_id, _s.lead_article_id, _s.importance, _s.article_count, _s.redirect_to,
     _s.category, _s.first_published_at, _s.updated_at, _s.pushed_at, _len(_s.pulse).label("len_pulse"),
     _len(_s.embedding).label("len_embedding"), _sig(*STORY_TEXT_COLUMNS).label("sig")],
    scope=lambda now: _s.updated_at >= now - timedelta(days=horizon_days()),
    keep=lambda r, now: (db.as_utc(r.updated_at) or now) >= now - timedelta(days=horizon_days()),
)


THREAD_TEXT_COLUMNS = [_t.slug, _t.title, _t.summary, _t.category, _t.entities]

THREADS = Mirror(
    "threads", db.threads,
    [_t.id, _t.status, _t.story_count, _t.named_count, _t.first_at, _t.updated_at,
     _len(_t.embedding).label("len_embedding"), _sig(*THREAD_TEXT_COLUMNS).label("sig")],
    scope=lambda now: _t.updated_at >= now - timedelta(days=horizon_days()),
    keep=lambda r, now: (db.as_utc(r.updated_at) or now) >= now - timedelta(days=horizon_days()),
)


def threads(conn) -> dict[int, tuple]:
    """Threads updated inside the export window (any status)."""
    return THREADS.rows(conn)


def articles(conn) -> dict[int, tuple]:
    """Articles created in the last 16 days (any status) and published or overflow ones in the export window."""
    return ARTICLES.rows(conn)


def stories(conn) -> dict[int, tuple]:
    """Stories updated inside the export window (any status)."""
    return STORIES.rows(conn)


# ---------------------------------------------------------------------------- details

class Details(_Store):
    def __init__(self, name: str, table, columns: list, max_age_days: float = DETAIL_MAX_AGE_DAYS):
        super().__init__(name)
        self.table, self.columns, self.max_age = table, columns, max_age_days * 86400
        self.names = [c.name for c in columns]
        self.Row = namedtuple(f"{name.title().replace('_', '')}Row", self.names)

    def empty(self) -> dict:
        return {"cols": self.names, "items": {}}

    def decode(self, data: dict) -> dict | None:
        if data.get("cols") != self.names:
            return None
        items = {int(k): [e[0], e[1], e[2], self.Row(*[_dec(v) for v in e[3]])] for k, e in (data.get("items") or {}).items()}
        return {"cols": self.names, "items": items}

    def encode(self, st: dict) -> dict:
        cutoff = time.time() - UNUSED_DAYS * 86400
        return {"cols": self.names, "items": {str(k): [e[0], e[1], e[2], [_enc(v) for v in e[3]]]
                                              for k, e in st["items"].items() if e[2] >= cutoff}}

    def get(self, conn, wanted: dict[int, list]) -> dict[int, tuple]:
        """wanted: id -> fingerprint (a list of plain values). Reads the rows that are missing, whose
        fingerprint changed, or that are due for their periodic re-read."""
        st = self.state(conn)
        items = st["items"]
        now = time.time()
        need = []
        for i, fp in wanted.items():
            e = items.get(i)
            # Spread the periodic re-reads over three days, so they do not all fall on one run.
            if e is None or e[0] != fp or now - e[1] > self.max_age + (i % 72) * 3600:
                need.append(i)
        for k in range(0, len(need), BATCH):
            for r in conn.execute(select(self.table.c.id, *self.columns).where(self.table.c.id.in_(need[k:k + BATCH]))).all():
                items[r[0]] = [wanted[r[0]], now, now, self.Row(*r[1:])]
        out = {}
        for i in wanted:
            e = items.get(i)
            if e is not None and e[0] == wanted[i]:  # a row that is gone keeps no stale copy
                e[2] = now
                out[i] = e[3]
        st["_dirty"] = True
        return out


ARTICLE_TEXT = Details("article_text", db.articles, ARTICLE_TEXT_COLUMNS)
ARTICLE_BODY = Details("article_body", db.articles, [_a.content_md])
STORY_TEXT = Details("story_text", db.stories, STORY_TEXT_COLUMNS)
STORY_TITLE = Details("story_title", db.stories, [_s.slug, _s.headline])
THREAD_TEXT = Details("thread_text", db.threads, THREAD_TEXT_COLUMNS)
TOPIC_TEXT = Details("topic_text", db.topics, [db.topics.c.slug, db.topics.c.name, db.topics.c.kind, db.topics.c.description])
RUNS = Details("runs", db.runs, [db.runs.c.started_at, db.runs.c.finished_at, db.runs.c.step, db.runs.c.stats], max_age_days=3650)


def article_text(conn, rows) -> dict[int, tuple]:
    """Summaries, titles and links of mirror article rows."""
    return ARTICLE_TEXT.get(conn, {r.id: [r.sig] for r in rows})


def article_bodies(conn, rows) -> dict[int, str]:
    """Article text (content_md) of mirror article rows that have some."""
    got = ARTICLE_BODY.get(conn, {r.id: [r.len_content_md] for r in rows if (r.len_content_md or 0) > 0})
    return {i: r.content_md for i, r in got.items() if r.content_md}


def story_text(conn, rows) -> dict[int, tuple]:
    return STORY_TEXT.get(conn, {r.id: [r.sig] for r in rows})


def story_titles(conn, rows) -> dict[int, tuple]:
    """Slug and headline only (the dashboard's lists)."""
    return STORY_TITLE.get(conn, {r.id: [r.sig] for r in rows})


def thread_text(conn, rows) -> dict[int, tuple]:
    """Slug, title, summary, category and entities of mirror thread rows."""
    return THREAD_TEXT.get(conn, {r.id: [r.sig] for r in rows})


def described_topics(conn) -> list[tuple]:
    """Topics and pages with a description (slug, name, kind, description), in id order. Only the
    ones whose description is new or rewritten since the last run are read in full."""
    t = db.topics.c
    marks = conn.execute(select(t.id, t.described_at_count, _sig(t.description, t.name, t.slug, t.kind))
                         .where(t.description.isnot(None)).order_by(t.id)).all()
    got = TOPIC_TEXT.get(conn, {i: [n, sig] for i, n, sig in marks})
    return [got[i] for i, _n, _sig_value in marks if i in got]


def merged(*rows) -> SimpleNamespace:
    out: dict = {}
    for r in rows:
        if r is not None:
            out.update(r._asdict())
    return SimpleNamespace(**out)


# ---------------------------------------------------------------------------- vectors

class Vectors(_Store):
    suffix = ".npz"

    def __init__(self, name: str, table, column: str = "embedding"):
        super().__init__(name)
        self.table, self.column = table, column

    def empty(self) -> dict:
        return {"items": {}}

    def load(self, path: Path) -> dict | None:
        try:
            with np.load(path, allow_pickle=False) as z:
                if int(z["format"]) != FORMAT:
                    return None
                items: dict[int, list] = {}
                for key in z.files:
                    if not key.endswith("_ids"):
                        continue
                    g = key[:-4]
                    vecs = z[g + "_vecs"] if g != "none" else None
                    for j, (i, fp, seen) in enumerate(zip(z[key], z[g + "_fps"], z[g + "_seen"])):
                        items[int(i)] = [str(fp), float(seen), None if vecs is None else vecs[j]]
                return {"items": items}
        except (OSError, ValueError, KeyError, EOFError):
            return None

    def write(self, path: Path, st: dict) -> None:
        cutoff = time.time() - UNUSED_DAYS * 86400
        groups: dict[str, list] = {}
        for i, (fp, seen, vec) in st["items"].items():
            if seen >= cutoff:
                groups.setdefault("none" if vec is None else f"d{len(vec)}", []).append((i, fp, seen, vec))
        arrays = {"format": np.asarray(FORMAT)}
        for g, entries in groups.items():
            arrays[g + "_ids"] = np.asarray([e[0] for e in entries], dtype=np.int64)
            arrays[g + "_fps"] = np.asarray([e[1] for e in entries], dtype=str)
            arrays[g + "_seen"] = np.asarray([e[2] for e in entries], dtype=np.float64)
            if g != "none":
                arrays[g + "_vecs"] = np.stack([e[3] for e in entries]).astype(np.float16)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp.npz")
        np.savez(tmp, **arrays)
        os.replace(tmp, path)

    def get(self, conn, wanted: dict[int, str]) -> dict[int, np.ndarray | None]:
        """wanted: id -> fingerprint string. Returns float32 vectors (None where the row has none)."""
        st = self.state(conn)
        items = st["items"]
        now = time.time()
        need = [i for i, fp in wanted.items() if i not in items or items[i][0] != fp]
        col = self.table.c[self.column]
        for k in range(0, len(need), BATCH):
            for rid, value in conn.execute(select(self.table.c.id, col).where(self.table.c.id.in_(need[k:k + BATCH]))).all():
                v = db.unpack_vec(value)
                items[rid] = [wanted[rid], now, None if v is None else v.astype(np.float16)]
        out = {}
        for i in wanted:
            e = items.get(i)
            if e is not None and e[0] == wanted[i]:
                e[1] = now
                out[i] = None if e[2] is None else e[2].astype(np.float32)
        if need or wanted:
            st["_dirty"] = True
        return out


ARTICLE_VECTORS = Vectors("article_vectors", db.articles)
STORY_VECTORS = Vectors("story_vectors", db.stories)
THREAD_VECTORS = Vectors("thread_vectors", db.threads)


def article_vectors(conn, rows) -> dict[int, np.ndarray | None]:
    """Embeddings of mirror article rows (float16 precision)."""
    return ARTICLE_VECTORS.get(conn, {r.id: f"{r.len_embedding}|{iso(r.created_at)}" for r in rows})


def story_vectors(conn, rows) -> dict[int, np.ndarray | None]:
    """Embeddings of mirror story rows: a merge changes the article count and updated_at, a repair the count."""
    return STORY_VECTORS.get(conn, {r.id: f"{r.len_embedding}|{r.article_count}|{iso(r.updated_at)}" for r in rows})


def thread_vectors(conn, rows) -> dict[int, np.ndarray | None]:
    return THREAD_VECTORS.get(conn, {r.id: f"{r.len_embedding}|{r.story_count}|{iso(r.updated_at)}" for r in rows})


# ---------------------------------------------------------------------------- small things

class Blob(_Store):
    """A small JSON document (the ranking model, known article addresses)."""

    def get(self, conn) -> dict:
        return self.state(conn)

    def put(self, conn, data: dict) -> None:
        st = self.state(conn)
        for k in [k for k in st if not k.startswith("_")]:
            del st[k]
        st.update(data)
        st["_dirty"] = True


RANK_MODEL = Blob("rank_model")
ENGAGEMENT = Blob("engagement")
KNOWN_URLS = Blob("known_urls")
SUSPECTS = Blob("suspect_duplicates")  # story pairs that may be one event (merge.py), for the dashboard


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def known_urls(conn) -> dict[str, float]:
    """Hashes of article addresses already in the database (fetch.py adds each one it inserts)."""
    st = KNOWN_URLS.state(conn)
    urls = st.setdefault("urls", {})
    cutoff = time.time() - URL_DAYS * 86400
    if st.get("pruned_at", 0) < time.time() - 3600:
        for h in [h for h, t in urls.items() if t < cutoff]:
            del urls[h]
        st["pruned_at"] = time.time()
    st["_dirty"] = True
    return urls
