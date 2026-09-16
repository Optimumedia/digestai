"""Database reads: the read meter, the runner's cache (incremental equals full), embedding storage
and storage clean-up. Offline, on temporary SQLite databases: python tests/test_reads.py
(or on Postgres with TEST_POSTGRES_URL, as the postgres-tests workflow does)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, delete, insert, select, update  # noqa: E402

from digest import cache, config, db  # noqa: E402

NOW = datetime.now(timezone.utc)


@contextmanager
def fresh_db(tracking: bool = True):
    """A temporary database as db.engine() would set it up, its own cache directory, and the
    module globals pointed at them for the duration."""
    tmp = Path(tempfile.mkdtemp())
    saved = (db._engine, config.CACHE_DIR, config.SITE_DATA_DIR)
    pg = os.environ.get("TEST_POSTGRES_URL")  # set by .github/workflows/postgres-tests.yml
    if pg:
        eng = create_engine(pg, future=True)
        with eng.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
        db._tracking_ready.clear()
    else:
        eng = create_engine(f"sqlite:///{(tmp / 'test.db').as_posix()}", future=True)
    db.install_read_meter(eng)
    db.metadata.create_all(eng)
    if tracking:
        assert db.install_change_tracking(eng)
    db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = eng, tmp / "cache", tmp / "site"
    cache.reset()
    try:
        yield eng, tmp
    finally:
        db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = saved
        cache.reset()
        eng.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def new_process():
    """What the next run sees: the cache files on disk, nothing in memory."""
    cache.save_all()
    cache.reset()


def unit(rng, dim=384, base=None, noise=0.3):
    v = rng.standard_normal(dim)
    if base is not None:
        v = base + noise * v / np.sqrt(dim)
    return (v / np.linalg.norm(v)).astype(np.float32)


def seed(eng, stories: int = 12, per_story: int = 3, rng=None) -> None:
    rng = rng or np.random.default_rng(1)
    with eng.begin() as conn:
        conn.execute(insert(db.sources), [
            {"id": 1, "key": "lab", "name": "Lab", "url": "https://lab.test/feed", "source_type": "primary"},
            {"id": 2, "key": "hn", "name": "HN", "url": "https://hn.test", "source_type": "community"},
            {"id": 3, "key": "press", "name": "Press", "url": "https://press.test/feed", "source_type": "press"}])
        conn.execute(insert(db.threads), [
            {"id": 1, "slug": "saga", "title": "A saga", "summary": "It goes on.", "category": "models",
             "entities": {"companies": ["Acme"]}, "embedding": db.pack_vec(unit(rng)), "story_count": 3, "named_count": 2,
             "first_at": NOW - timedelta(days=3), "updated_at": NOW - timedelta(days=1), "status": "published"}])
        aid = 0
        for s in range(1, stories + 1):
            base = unit(rng, noise=0)
            first = NOW - timedelta(hours=6 * s)
            conn.execute(insert(db.stories).values(
                id=s, slug=f"story-{s}", headline=f"Story {s}", summary_md=f"Summary of story {s}. " * 5,
                key_points=[f"Point {s}.1", f"Point {s}.2"], why_it_matters="Because.", category="models",
                entities={"companies": ["Acme", f"Co{s}"], "models": [], "people": []}, lead_article_id=aid + 1,
                article_count=per_story, importance=5 + s % 4, score=0.5, embedding=db.pack_vec(base), status="published",
                thread_id=1 if s <= 3 else None, pulse="People like it." if s == 2 else None,
                first_published_at=first, updated_at=first + timedelta(hours=1)))
            for k in range(per_story):
                aid += 1
                conn.execute(insert(db.articles).values(
                    id=aid, url=f"https://pub{k}.test/{aid}", source_id=1 + (aid % 3), story_id=s, slug=f"article-{aid}",
                    title=f"Title {aid}", raw_title=f"Title {aid}", headline=f"Headline {aid}", author="Writer",
                    domain="openai.com" if aid % 5 == 0 else f"pub{k}.test", published_at=first + timedelta(minutes=10 * k),
                    fetched_at=first, created_at=first, description=f"Description {aid}", content_md=f"# Text {aid}\n\n" + "words " * 200,
                    content_text="words " * 200, feed_content="feed " * 50, word_count=200 if aid % 7 else 0, image_url=f"https://img.test/{aid}.jpg",
                    show_fulltext=aid % 4 != 0, status="published", simhash=int(rng.integers(-2**62, 2**62)),
                    summary_md=f"Article summary {aid}.", key_points=[f"K{aid}"], why_it_matters="It matters.", category="models",
                    entities={"companies": ["Acme"], "models": [f"M{aid}"], "people": []}, content_type="news", importance=4 + aid % 5,
                    enrich_model="groq:x", embedding=db.pack_vec(unit(rng, base=base)), predicted_score=0.1 * (aid % 10),
                    engagement=0.0, discussion_site="hn" if aid % 6 == 0 else None,
                    discussion_url=f"https://news.ycombinator.com/item?id={aid}" if aid % 6 == 0 else None,
                    discussion_points=aid * 3 if aid % 6 == 0 else None,
                    model_release={"name": f"Model {aid}", "lab": "Acme", "availability": "api"} if aid % 9 == 0 else None,
                    funding={"company": "Acme", "amount_usd": 1e8, "round": "series b"} if aid % 11 == 0 else None))
        if eng.dialect.name == "postgresql":  # explicit ids do not move Postgres sequences
            for t in ("sources", "threads", "stories", "articles"):
                conn.exec_driver_sql(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}))")
        for r in range(40):  # rejected articles keep their text until tidy empties it
            conn.execute(insert(db.articles).values(
                url=f"https://rej.test/{r}", source_id=3, title=f"Off topic {r}", domain="rej.test", fetched_at=NOW, created_at=NOW,
                status="rejected", reject_reason="gate: not about AI (score 1.0)", content_md="x " * 500, content_text="x " * 500,
                feed_content="y " * 100, simhash=r))


# ---------------------------------------------------------------------------- read meter

def test_read_meter_counts_rows_on_sqlite_and_postgres_results():
    with fresh_db() as (eng, _tmp):
        with eng.begin() as conn:
            conn.execute(insert(db.sources).values(key="k", name="N", url="https://x.test/" + "a" * 1000))
        before = db.bytes_read()
        with eng.connect() as conn:
            conn.execute(select(db.sources.c.url)).all()
        got = db.bytes_read() - before
        assert 1000 < got < 1300, got  # the url's 1,015 bytes plus the row overhead each driver reports
        before = db.bytes_read()
        with eng.connect() as conn:
            conn.execute(select(db.sources.c.id)).all()
        got = db.bytes_read() - before
        assert got < 120, got

    class FakeResult:  # psycopg's pgresult: row count, column count and each value's length
        ntuples, nfields = 2, 3

        def get_length(self, r, c):
            return [[5, 0, 100], [7, 1, 2000]][r][c]

    assert db.pgresult_bytes(FakeResult()) == db.PG_QUERY_OVERHEAD + 2 * (db.PG_ROW_OVERHEAD + 3 * db.PG_COLUMN_OVERHEAD) + 2113
    assert db.pgresult_bytes(None) == 0


# ---------------------------------------------------------------------------- embeddings

def test_packed_embeddings_are_small_and_keep_similarities():
    rng = np.random.default_rng(3)
    base = unit(rng, noise=0)
    vecs = [unit(rng, base=base, noise=float(n)) for n in np.linspace(0.1, 3.0, 60)]
    packed = [db.pack_vec(v) for v in vecs]
    assert all(len(json.dumps(p)) == 1030 for p in packed)  # as stored; ~8,500 characters as a JSON list
    assert len(json.dumps([float(x) for x in vecs[0]])) > 7000
    worst = 0.0
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            exact = float(np.dot(vecs[i], vecs[j]))
            approx = float(np.dot(db.unpack_vec(packed[i]), db.unpack_vec(packed[j])))
            worst = max(worst, abs(exact - approx))
    # The clustering margins are 0.03 and more; float16 moves a similarity by far less.
    assert worst < 1e-3, worst
    # Rows written before keep their JSON lists (or JSON text); both read the same.
    as_list = [float(x) for x in vecs[0]]
    assert np.allclose(db.unpack_vec(as_list), vecs[0]) and np.allclose(db.unpack_vec(json.dumps(as_list)), vecs[0])
    assert db.unpack_vec(None) is None and db.unpack_vec([]) is None and db.unpack_vec("") is None


# ---------------------------------------------------------------------------- change tracking

def test_change_tracking_stamps_writes_and_records_deletions():
    with fresh_db() as (eng, _tmp):
        seed(eng, stories=2, per_story=1)
        with eng.connect() as conn:
            wm = db.watermark(conn)
            rev = conn.execute(select(db.articles.c.rev).where(db.articles.c.id == 1)).scalar()
        assert rev is not None and rev < wm, (rev, wm)
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id == 1).values(predicted_score=0.9))
        with eng.connect() as conn:
            rev1 = conn.execute(select(db.articles.c.rev).where(db.articles.c.id == 1)).scalar()
            assert rev1 >= wm, (rev1, wm)
            wm2 = db.watermark(conn)
        with eng.begin() as conn:
            with db.revision_kept(conn):
                conn.execute(update(db.articles).where(db.articles.c.id == 2).values(content_text=None))
            # A story without articles, so Postgres's foreign key allows deleting it.
            conn.execute(insert(db.stories).values(id=99, slug="gone", headline="Gone", status="published", first_published_at=NOW, updated_at=NOW))
            conn.execute(delete(db.stories).where(db.stories.c.id == 99))
        with eng.connect() as conn:
            rev2 = conn.execute(select(db.articles.c.rev).where(db.articles.c.id == 2)).scalar()
            assert rev2 < wm2, (rev2, wm2)  # clean-up is invisible
            gone = conn.execute(select(db.deleted_rows.c.table_name, db.deleted_rows.c.row_id, db.deleted_rows.c.rev)).all()
            assert [(t, i) for t, i, _r in gone] == [("stories", 99)] and gone[0][2] >= wm2, (gone, wm2)
            assert db.change_tracking_ready(conn), "tracking not ready"
        # A second install is harmless (CREATE ... IF NOT EXISTS).
        assert db.install_change_tracking(eng), "second install"


# ---------------------------------------------------------------------------- mirror

def _full(mirror, conn):
    now = db.utcnow()
    return {r[0]: mirror.Row(*r) for r in conn.execute(select(*mirror.columns).where(mirror.scope(now))).all()
            if mirror.keep(mirror.Row(*r), now)}


def test_mirror_reads_only_changes_and_matches_a_full_read():
    with fresh_db() as (eng, _tmp):
        seed(eng)
        with eng.connect() as conn:
            first = cache.articles(conn)
        new_process()
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id == 3).values(status="rejected", reject_reason="moderation: domain"))
            conn.execute(update(db.articles).where(db.articles.c.id == 4).values(summary_md="A much longer summary than before."))
            conn.execute(delete(db.articles).where(db.articles.c.id == 5))
            conn.execute(insert(db.articles).values(url="https://new.test/1", title="New", fetched_at=NOW, created_at=NOW, status="new"))
            conn.execute(update(db.articles).where(db.articles.c.id == 6).values(created_at=NOW - timedelta(days=400)))  # leaves the window
        before = db.bytes_read()
        with eng.connect() as conn:
            got = cache.articles(conn)
            cost = db.bytes_read() - before
            expected = _full(cache.ARTICLES, conn)
        assert got == expected
        assert got[3].status == "rejected" and got[4].sig != first[4].sig and 5 not in got and 6 not in got
        # The incremental read costs a fraction of the full one.
        before = db.bytes_read()
        with eng.connect() as conn:
            _full(cache.ARTICLES, conn)
        assert cost * 4 < db.bytes_read() - before, (cost, db.bytes_read() - before)


def test_cache_falls_back_to_a_full_read_when_missing_corrupt_or_without_tracking():
    with fresh_db() as (eng, tmp):
        seed(eng)
        with eng.connect() as conn:
            cache.stories(conn)
        new_process()
        for f in (tmp / "cache").rglob("*"):
            if f.is_file():
                f.write_bytes(b"not a cache file")
        with eng.begin() as conn:
            conn.execute(update(db.stories).where(db.stories.c.id == 1).values(score=0.99))
        with eng.connect() as conn:
            assert cache.stories(conn) == _full(cache.STORIES, conn)
        new_process()
        shutil.rmtree(tmp / "cache")
        with eng.connect() as conn:
            assert cache.stories(conn)[1].score == 0.99
    with fresh_db(tracking=False) as (eng, _tmp):
        seed(eng)
        with eng.connect() as conn:
            assert not db.change_tracking_ready(conn)
            cache.stories(conn)
        with eng.begin() as conn:
            conn.execute(update(db.stories).where(db.stories.c.id == 1).values(score=0.42))  # no revision stamped
        with eng.connect() as conn:
            assert cache.stories(conn)[1].score == 0.42


# ---------------------------------------------------------------------------- export

def _export_files(site: Path) -> dict:
    out = {}
    for f in sorted(site.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.pop("generatedAt", None)
        out[f.name] = data
    return out


def _export_both_ways(eng, tmp: Path) -> tuple[dict, dict]:
    """The export with the cache the earlier runs left, and with no cache at all."""
    from digest import export

    config.SITE_DATA_DIR = tmp / "site-warm"
    new_process()
    export.run()
    warm = _export_files(config.SITE_DATA_DIR)
    new_process()
    saved = config.CACHE_DIR
    config.CACHE_DIR = tmp / "cache-cold"
    shutil.rmtree(config.CACHE_DIR, ignore_errors=True)
    cache.reset()
    config.SITE_DATA_DIR = tmp / "site-cold"
    export.run()
    cold = _export_files(config.SITE_DATA_DIR)
    cache.reset()
    config.CACHE_DIR = saved
    return warm, cold


def test_incremental_export_equals_a_full_export_across_runs():
    with fresh_db() as (eng, tmp):
        seed(eng)
        warm, cold = _export_both_ways(eng, tmp)
        assert warm == cold and len(warm["stories.json"]) == 12
        # Run 2: a new story, a merge into story 1 with a new lead, a pulse, a discussion, new predictions,
        # an unpublished story, a deleted story, a renamed thread and a rewritten article text.
        with eng.begin() as conn:
            conn.execute(insert(db.stories).values(id=50, slug="story-50", headline="Brand new", summary_md="New.", key_points=["n"],
                                                   category="agents", entities={"companies": ["Newco"]}, lead_article_id=500, article_count=1,
                                                   importance=8, score=0.9, status="published", first_published_at=NOW, updated_at=NOW))
            conn.execute(insert(db.articles).values(id=500, url="https://new.test/500", source_id=1, story_id=50, slug="article-500", title="New",
                                                    headline="New", domain="new.test", published_at=NOW, fetched_at=NOW, created_at=NOW,
                                                    content_md="fresh text", word_count=2, show_fulltext=True, status="published",
                                                    summary_md="new", enrich_model="groq:x", engagement=0.0))
            conn.execute(insert(db.articles).values(id=501, url="https://new.test/501", source_id=3, story_id=1, slug="article-501", title="Follow-up",
                                                    headline="Follow-up", domain="press.test", published_at=NOW, fetched_at=NOW, created_at=NOW,
                                                    content_md="follow-up text", word_count=3, show_fulltext=True, status="published",
                                                    summary_md="more", importance=9, enrich_model="groq:x", engagement=0.0))
            conn.execute(update(db.stories).where(db.stories.c.id == 1).values(
                article_count=4, updated_at=NOW, headline="Story 1, updated", summary_md="Now with a follow-up.", lead_article_id=501))
            conn.execute(update(db.stories).where(db.stories.c.id == 3).values(pulse="Mixed reactions.", pulse_at=NOW))
            conn.execute(update(db.articles).where(db.articles.c.id == 7).values(
                discussion_site="hn", discussion_url="https://news.ycombinator.com/item?id=77", discussion_points=321))
            conn.execute(update(db.articles).where(db.articles.c.id.in_([8, 9, 10])).values(predicted_score=0.77))
            conn.execute(update(db.stories).where(db.stories.c.id == 4).values(status="unpublished"))
            conn.execute(delete(db.articles).where(db.articles.c.story_id == 5))
            conn.execute(delete(db.stories).where(db.stories.c.id == 5))
            conn.execute(update(db.threads).where(db.threads.c.id == 1).values(title="The saga, renamed", summary="Where it stands.", named_count=4))
            conn.execute(update(db.articles).where(db.articles.c.id == 16).values(content_md="rewritten text, longer than before " * 3))
        warm, cold = _export_both_ways(eng, tmp)
        assert warm == cold
        stories = {s["id"]: s for s in warm["stories.json"]}
        assert 50 in stories and 4 not in stories and 5 not in stories
        assert stories[1]["headline"] == "Story 1, updated" and stories[1]["leadArticleId"] == 501
        assert stories[3]["pulse"] == "Mixed reactions."
        assert warm["threads.json"][0]["title"] == "The saga, renamed"
        # Run 3: the storage clean-up empties columns nothing exports; the output does not change.
        from digest import tidy

        assert tidy.clean(eng, limit=10000)
        warm2, cold2 = _export_both_ways(eng, tmp)
        assert warm2 == cold2 == warm


# ---------------------------------------------------------------------------- tidy

def test_tidy_empties_unused_columns_in_bounded_batches():
    from digest import tidy

    with fresh_db() as (eng, _tmp):
        seed(eng, stories=10, per_story=3)  # 30 published + 40 rejected articles, all with text
        with eng.connect() as conn:
            wm = db.watermark(conn)
        done = tidy.clean(eng, limit=25)
        assert sum(done.values()) == 25 and done == {"summarised_text": 25}
        total = 25
        while True:
            done = tidy.clean(eng, limit=25)
            if not done:
                break
            assert sum(done.values()) <= 25
            total += sum(done.values())
        a = db.articles.c
        with eng.connect() as conn:
            assert conn.execute(select(a.id).where(a.content_text.isnot(None))).first() is None
            assert conn.execute(select(a.id).where(a.feed_content.isnot(None))).first() is None
            # Published articles keep the text their pages show; rejected ones lose it and any embedding.
            assert conn.execute(select(a.id).where(a.status == "published", a.content_md.is_(None))).first() is None
            assert conn.execute(select(a.id).where(a.status == "rejected", a.content_md.isnot(None))).first() is None
            # None of it looks like a change to the runner's copy.
            assert conn.execute(select(a.id).where(a.rev >= wm)).first() is None
        assert total == 70 + 40


# ---------------------------------------------------------------------------- rank

def test_rank_reuses_its_model_and_counts_engagement_incrementally():
    from digest import rank

    with fresh_db() as (eng, _tmp):
        seed(eng, stories=20, per_story=3)
        with eng.begin() as conn:
            for i in range(1, 61):
                conn.execute(update(db.articles).where(db.articles.c.id == i).values(discussion_points=i * 7, trend_score=i % 5))
            conn.execute(insert(db.events), [{"article_id": 1 + i % 30, "type": "view", "value": 1, "session": f"s{i}", "created_at": NOW - timedelta(hours=i)}
                                             for i in range(80)])
        with eng.begin() as conn:
            first = rank.train_and_predict(conn)
        assert first["trained"] and first["refitted"] and first["predictions_changed"] > 0
        new_process()
        with eng.begin() as conn:
            again = rank.train_and_predict(conn)
        assert again["trained"] and not again["refitted"] and again["predictions_changed"] == 0
        # New reader events: the incremental count equals a full recount.
        with eng.begin() as conn:
            conn.execute(insert(db.events), [{"article_id": 2, "type": "dwell", "value": 400, "session": "late", "created_at": NOW},
                                             {"article_id": 2, "type": "dwell", "value": 400, "session": "late", "created_at": NOW},
                                             {"article_id": 40, "type": "share", "value": 1, "session": "x", "created_at": NOW}])
        new_process()
        with eng.begin() as conn:
            incremental = rank._engagement(conn)
        cache.ENGAGEMENT.put(eng.connect(), {})
        with eng.begin() as conn:
            full = rank._engagement(conn)
        assert incremental == full and full[2] > full[3] and 40 in full


# ---------------------------------------------------------------------------- admin

def test_database_summary_cycle_and_cards():
    from digest import admin

    assert admin.billing_cycle(datetime(2026, 9, 16, tzinfo=timezone.utc)) == (datetime(2026, 9, 11).date(), datetime(2026, 10, 11).date())
    assert admin.billing_cycle(datetime(2026, 9, 3, tzinfo=timezone.utc)) == (datetime(2026, 8, 11).date(), datetime(2026, 9, 11).date())
    assert admin.billing_cycle(datetime(2027, 1, 20, tzinfo=timezone.utc), day=31) == (datetime(2026, 12, 31).date(), datetime(2027, 1, 31).date())
    now = datetime(2026, 9, 16, 12, 30, tzinfo=timezone.utc)
    rows = []
    for h in range(6):  # six hourly runs of two steps each; the newest is still running
        t = now - timedelta(hours=h, minutes=20)
        rows += [{"step": "fetch", "startedAt": t, "stats": {"readKB": 100.0}},
                 {"step": "export", "startedAt": t + timedelta(minutes=5), "stats": {"readKB": 900.0}}]
    history = [{"day": "2026-09-10", "dbReadKb": 99999.0}, {"day": "2026-09-15", "dbReadKb": 2048.0}, {"day": "2026-09-16", "dbReadKb": 1024.0}]
    d = admin.database_summary({"bytes": 380 * 1048576, "tables": [{"name": "articles", "bytes": 300 * 1048576}]}, rows, history, now, "postgres")
    assert d["sizeMB"] == 380 and d["tables"] == [{"name": "articles", "mb": 300.0}]
    assert d["reads"]["latestRunKB"] == 1000 and d["reads"]["steps"][0] == {"step": "export", "readKB": 900.0}
    assert d["cycle"]["start"] == "2026-09-11" and d["cycle"]["measuredMB"] == 3.0 and d["cycle"]["measuredSince"] == "2026-09-15"
    cards = {c["id"]: c for c in admin.database_cards(d)}
    assert cards["database:size"]["level"] == "warning" and "database:reads" not in cards  # 1 MB x 48 x 30 = 1.4 GB
    d["sizeMB"], d["reads"]["monthlyMB"] = 460, 4500
    cards = {c["id"]: c for c in admin.database_cards(d)}
    assert cards["database:size"]["level"] == "critical" and cards["database:reads"]["level"] == "warning"


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - report every test, not just the first error
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
