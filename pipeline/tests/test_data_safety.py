"""Runner files that are damaged never crash a step or stop publishing, and the nightly backup really
restores into a new database. Offline, on temporary SQLite databases: python tests/test_data_safety.py

From 7 to 23 Nov 2026 a damaged archive copy in the runner cache ("invalid block type", a zlib.error)
crashed the export on every run and the site froze for 16 days. These tests damage every file the
runner keeps between runs, in every way a copy can break (empty, random bytes, flipped bits inside a
gzip stream, cut short, not JSON, JSON of the wrong shape), and check each reader treats it as missing.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, func, select  # noqa: E402

from digest import archive, backup, cache, config, db  # noqa: E402

NOW = datetime.now(timezone.utc)


def _gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj).encode("utf-8"))


def _flipped(obj) -> bytes:
    """A gzip stream with bytes changed inside the compressed data: what the archive copy looked like."""
    raw = bytearray(gzip.compress(json.dumps(obj).encode("utf-8") * 20))
    rng = random.Random(4)
    for i in rng.sample(range(12, len(raw) - 8), 6):
        raw[i] ^= 0xFF
    return bytes(raw)


def damaged_gz(good) -> list[tuple[str, bytes]]:
    whole = _gz(good)
    return [("empty", b""), ("random", bytes(random.Random(1).randrange(256) for _ in range(300))),
            ("flipped", _flipped(good)), ("cut short", whole[: len(whole) // 2]),
            ("not json", gzip.compress(b"{not json")), ("a list", _gz([1, 2, 3])), ("a string", _gz("x")),
            ("not utf-8", gzip.compress(b"\xff\xfe\x00{")), ("deep", gzip.compress(b"[" * 100000))]


def damaged_json() -> list[tuple[str, bytes]]:
    return [("empty", b""), ("random", bytes(random.Random(2).randrange(256) for _ in range(300))),
            ("cut short", b'{"format": 1, "ro'), ("a list", b"[1, 2]"), ("a string", b'"x"'), ("not utf-8", b"\xff\xfe{")]


def test_read_file_never_raises_and_moves_damage_aside():
    tmp = Path(tempfile.mkdtemp())
    good = {"format": 1, "rows": {"a": 1}}
    for label, raw in damaged_gz(good):
        p = tmp / "thing.json.gz"
        p.write_bytes(raw)
        cache.DAMAGED.clear()
        assert cache.read_file(p, dict) is None, label
        assert not p.exists() and (tmp / "thing.json.gz.bad").exists(), label
        assert cache.DAMAGED == ["thing.json.gz"], (label, cache.DAMAGED)
    # Missing is not damage.
    cache.DAMAGED.clear()
    assert cache.read_file(tmp / "missing.json.gz", dict) is None and cache.DAMAGED == []
    # A good file round-trips, and a content check can refuse one.
    cache.write_file(tmp / "ok.json.gz", good)
    assert cache.read_file(tmp / "ok.json.gz", dict) == good
    assert cache.read_file(tmp / "ok.json.gz", dict, check=lambda d: d["format"] == 2) is None
    cache.write_file(tmp / "plain.json", [1, 2], gz=False)
    assert cache.read_file(tmp / "plain.json", list) == [1, 2]


def test_every_cache_store_takes_a_damaged_copy_as_missing():
    import test_reads as tr

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng)
        with eng.connect() as conn:
            cache.articles(conn), cache.stories(conn), cache.threads(conn)
            cache.article_vectors(conn, list(cache.articles(conn).values()))
            cache.known_urls(conn)["abc"] = 1.0
            cache.RANK_MODEL.put(conn, {"weights": [1.0], "trained_at": 1.0, "dim": 1})
            cache.save_all()
            key = cache.db_key(conn)
        stores = list(cache._Store.registry)
        assert len(stores) >= 15
        for store in stores:
            variants = damaged_gz({"format": 1, "rows": "x", "wm": "x", "items": {"1": "x"}, "cols": ["id"]})
            if isinstance(store, cache.Blob) and store.shapes:
                bad_key = next(iter(store.shapes))
                variants.append(("wrong inner shape", _gz({"format": 1, bad_key: "a string"})))
            if isinstance(store, cache.Mirror):
                variants.append(("wrong watermark", _gz({"format": 1, "cols": store.names, "wm": "x", "full_at": 0, "rows": []})))
                variants.append(("short row", _gz({"format": 1, "cols": store.names, "wm": 1, "full_at": 0, "rows": [[1]]})))
            for label, raw in variants:
                p = store._path(key)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                cache.reset()
                with eng.connect() as conn:
                    st = store.state(conn)
                    assert {k: v for k, v in st.items() if not k.startswith("_")} == store.empty(), (store.name, label)
        # And every read through the cache still answers from the database.
        cache.reset()
        with eng.connect() as conn:
            assert len(cache.stories(conn)) == 12 and len(cache.articles(conn)) == 76
            vecs = cache.article_vectors(conn, list(cache.articles(conn).values()))
            assert sum(v is not None for v in vecs.values()) == 36


def test_vectors_with_mismatched_arrays_are_damaged():
    import numpy as np

    tmp = Path(tempfile.mkdtemp())
    p = tmp / "v.npz"
    np.savez(p, format=np.asarray(cache.FORMAT), d4_ids=np.asarray([1, 2]), d4_fps=np.asarray(["a", "b"]),
             d4_seen=np.asarray([1.0, 2.0]), d4_vecs=np.zeros((1, 4), dtype=np.float16))
    assert cache.ARTICLE_VECTORS.load(p) is None and (tmp / "v.npz.bad").exists()


def test_other_runner_files_take_damage_as_missing():
    from digest import audio, howto, images, media, morning, prices, releases, run, simplify, team

    tmp = Path(tempfile.mkdtemp())
    saved = (config.CACHE_DIR, config.SITE_DATA_DIR, howto.CACHE_FILE, simplify.CACHE_FILE)
    config.CACHE_DIR, config.SITE_DATA_DIR = tmp / "cache", tmp / "site"
    howto.CACHE_FILE, simplify.CACHE_FILE = config.CACHE_DIR / "howto.json.gz", config.CACHE_DIR / "simplify.json.gz"
    os.environ["BACKUP_STORE_DIR"] = str(tmp / "backup-store")
    try:
        gz_readers = [
            (archive.path, lambda: archive._load()["stories"] == {}),
            (lambda: backup.tables_dir() / "stories.json.gz", lambda: backup._load("stories") == {}),
            (lambda: howto.CACHE_FILE, lambda: howto.load_cache() == {}),
            (lambda: simplify.CACHE_FILE, lambda: simplify.load_cache() == {}),
            (lambda: tmp / "prices.json.gz", lambda: prices.load(tmp / "prices.json.gz")["tools"] == {}),
            (lambda: tmp / "pages.json.gz", lambda: prices.load_page_cache(tmp / "pages.json.gz") == {}),
        ]
        for where, reads_empty in gz_readers:
            for label, raw in damaged_gz({"format": 1, "stories": {"a": "x"}, "windowStart": "not a date", "tools": []}):
                p = where()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                assert reads_empty(), (p.name, label)
        json_readers = [
            (lambda: backup.store_dir() / "state.json", lambda: backup._state() == {}),
            (releases.path, lambda: releases.read_file() is None),
            (lambda: media.store_dir() / "manifest.json", lambda: (media.reset(), media.manifest()["assets"] == {})[1]),
            (morning.store_path, lambda: morning.load_notes() == []),
            (lambda: config.CACHE_DIR / "team.json", lambda: team._read(config.CACHE_DIR / "team.json", None) is None),
            (lambda: config.CACHE_DIR / "work" / "plan.json", lambda: isinstance(audio._read_plan(config.CACHE_DIR / "work", ["a"]), dict)),
            (lambda: config.CACHE_DIR / "schema.key", lambda: db._read_schema_marker() is None or True),
        ]
        for where, reads_empty in json_readers:
            for label, raw in damaged_json():
                p = where()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                assert reads_empty(), (p.name, label)
        # The primary-image cache: a damaged copy is an empty one.
        for label, raw in damaged_json():
            p = config.CACHE_DIR / "primary.json"
            p.write_bytes(raw)
            images.primary_images([], NOW, fetch=lambda *a, **k: None, cache_path=p, out_path=tmp / "site" / "primary.json")
        # A damaged dashboard copy is never put in the site.
        for label, raw in damaged_json():
            (config.CACHE_DIR / "admin.json").write_bytes(raw)
            (config.SITE_DATA_DIR / "admin.json").unlink(missing_ok=True)
            assert run.restore_admin() is False and not (config.SITE_DATA_DIR / "admin.json").exists(), label
        (config.CACHE_DIR / "admin.json").write_text('{"generatedAt": "x"}', encoding="utf-8")
        assert run.restore_admin() is True
    finally:
        config.CACHE_DIR, config.SITE_DATA_DIR, howto.CACHE_FILE, simplify.CACHE_FILE = saved
        os.environ.pop("BACKUP_STORE_DIR", None)
        media.reset()


def test_export_still_publishes_with_every_runner_file_damaged():
    """The failure of November: whatever is in the runner cache, the export writes the site's data."""
    import test_reads as tr
    from digest import export

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng)
        config.SITE_DATA_DIR = tmp / "site-1"
        export.run()
        good = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        tr.new_process()
        files = [p for p in config.CACHE_DIR.rglob("*") if p.is_file()]
        assert any(p.name == "archive.json.gz" for p in files) and len(files) >= 5, [p.name for p in files]
        for p in files:
            p.write_bytes(_flipped({"format": 1, "x": list(range(50))}))
        cache.DAMAGED.clear()
        config.SITE_DATA_DIR = tmp / "site-2"
        stats = export.run()
        again = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        assert [s["slug"] for s in again] == [s["slug"] for s in good], stats
        assert "archive.json.gz" in cache.DAMAGED, cache.DAMAGED


# ---------------------------------------------------------------------------- backup restore

def _make_backup(eng, tmp: Path, passphrase: str) -> bytes:
    os.environ["BACKUP_STORE_DIR"] = str(tmp / "backup-store")
    try:
        with eng.connect() as conn:
            tables = backup.collect(conn)
    finally:
        os.environ.pop("BACKUP_STORE_DIR", None)
    return backup.encrypt(backup.pack(tables, NOW), passphrase)


def test_backup_restores_into_a_new_database_and_the_site_builds_from_it():
    import test_reads as tr
    from digest import export

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng)
        # A story merged into another keeps its row (its slug answers with a redirect): the backup
        # holds it without its texts, so the restore fills what it lacks from the column defaults.
        with eng.begin() as conn:
            conn.execute(db.stories.update().where(db.stories.c.id == 12).values(status="merged", redirect_to=11, pinned=True))
            conn.execute(db.articles.update().where(db.articles.c.story_id == 12).values(story_id=11))
        config.SITE_DATA_DIR = tmp / "site-original"
        export.run()
        original = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        blob = _make_backup(eng, tmp, "pass phrase")

        # The nightly check: the file opens and loads into a throwaway database.
        checked = backup.verify(blob, "pass phrase")
        assert checked["restored"]["stories"] == 12 and checked["check"]["publishedWithArticles"] == 11, checked
        # Rejected articles are only known by their small columns (no address): skipped, and said so.
        assert checked["restored"]["articles"] == 36 and checked["skipped"]["articles"] == 40, checked

        # A real restore into a new database, then the export from it.
        target = create_engine(f"sqlite:///{(tmp / 'restored.db').as_posix()}", future=True)
        db.install_read_meter(target)
        got = backup.restore(backup.unpack(backup.decrypt(blob, "pass phrase")), target)
        assert got["restored"]["sources"] == 3 and got["restored"]["threads"] == 1, got
        try:
            backup.restore(backup.unpack(backup.decrypt(blob, "pass phrase")), target)
            raise AssertionError("a restore into a database that has rows must refuse")
        except RuntimeError:
            pass
        with eng.connect() as a, target.connect() as b:
            for col in ("slug", "headline", "status", "redirect_to", "pinned", "category", "thread_id", "article_count", "importance"):
                q = lambda c: c.execute(select(db.stories.c.id, db.stories.c[col]).order_by(db.stories.c.id)).all()
                assert q(a) == q(b), col
            cols = [db.articles.c[c] for c in ("id", "url", "story_id", "title", "summary_md", "status", "domain", "source_id")]
            q = lambda c: c.execute(select(*cols).where(db.articles.c.status == "published").order_by(db.articles.c.id)).all()
            assert q(a) == q(b)
            # Dates come back as dates, and a restored row gets its revision from the tracking trigger.
            fp = b.execute(select(db.stories.c.first_published_at).where(db.stories.c.id == 1)).scalar()
            assert db.as_utc(fp) == db.as_utc(a.execute(select(db.stories.c.first_published_at).where(db.stories.c.id == 1)).scalar())
            q = lambda c: c.execute(select(db.stories.c.summary_md, db.stories.c.key_points).where(db.stories.c.status == "published").order_by(db.stories.c.id)).all()
            assert q(a) == q(b)
            # Ids continue after the restored rows.
            assert b.execute(select(func.max(db.stories.c.id))).scalar() == 12

        saved_engine = db._engine
        db._engine = target
        db.install_change_tracking(target)
        cache.reset()
        try:
            config.SITE_DATA_DIR = tmp / "site-restored"
            config.CACHE_DIR = tmp / "cache-restored"
            export.run()
            restored = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        finally:
            db._engine = saved_engine
            cache.reset()
            target.dispose()
        assert [s["slug"] for s in restored] == [s["slug"] for s in original]
        for a, b in zip(original, restored):
            for key in ("headline", "summaryMd", "keyPoints", "whyItMatters", "category", "articleCount", "threadId", "firstPublishedAt"):
                assert a[key] == b[key], (a["slug"], key)
            assert [x["url"] for x in a["articles"]] == [x["url"] for x in b["articles"]] and len(a["articles"]) >= 3


def test_backup_command_line_verifies_a_file():
    import contextlib
    import io

    import test_reads as tr

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng)
        blob = _make_backup(eng, tmp, "k")
        f = tmp / "b.enc"
        f.write_bytes(blob)
        os.environ["BACKUP_KEY"] = "k"
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert backup.main(["verify", str(f)]) == 0
        finally:
            os.environ.pop("BACKUP_KEY", None)
        assert json.loads(out.getvalue())["restored"]["stories"] == 12


if __name__ == "__main__":
    import logging

    logging.getLogger("digest").setLevel(logging.ERROR)  # the damaged-file warnings are expected here
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - report every test, not just the first error
                import traceback

                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
                traceback.print_exc(limit=4)
    sys.exit(1 if failures else 0)
