"""Step: a nightly, encrypted copy of the database's essential tables as a GitHub release asset.

Supabase's free plan has no downloadable backups. Once a day this step writes stories, articles
(without their text and embeddings), threads, sources, daily statistics, newsletters, topics,
social posts and model usage as JSON lines, packs them into a tar.gz, encrypts it with the
BACKUP_KEY passphrase in the format `openssl enc -aes-256-cbc -pbkdf2` produces (so a plain
openssl command restores it, see SETUP.md), and uploads it to the "backups" release. Assets
older than 30 days are deleted. Reader events and push subscriptions are never included.

Reads stay small: the story, article and thread rows come from the runner's copy of the
database (cache.py), which every run already brings up to date, and are remembered here after
they leave that copy's window; only the small tables are read in full each night. A backup
store that is missing (the cache was evicted) is seeded from the latest backup asset.

Restore: `openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -in FILE -out backup.tar.gz`
with the BACKUP_KEY passphrase, then `tar xzf backup.tar.gz` (one JSON object per line per table).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from . import cache, config, db, media

log = logging.getLogger("digest.backup")

FORMAT = 1
RELEASE_TAG = "backups"
KEEP_DAYS = 30
EVERY_HOURS = 20
PBKDF2_ITERATIONS = 200_000
SMALL_TABLES = ("sources", "daily_stats", "newsletters", "topics", "social_posts", "llm_usage")
TRACKED = ("stories", "articles", "threads")
NAME_RE = re.compile(r"^digest-backup-(\d{4}-\d{2}-\d{2})\.tar\.gz\.enc$")


def store_dir() -> Path:
    """When the last backup ran (small; rides in the read cache every run)."""
    return config.CACHE_DIR / "backup"


def tables_dir() -> Path:
    """The remembered table rows (a few MB): the workflow restores and saves this only on the run that
    makes the backup (marker file next to it)."""
    return Path(os.environ.get("BACKUP_STORE_DIR") or config.DATA_DIR / "backup-store")


def marker() -> Path:
    return tables_dir().with_name(tables_dir().name + ".new")


def due(now: datetime, st: dict | None = None) -> bool:
    last = (st if st is not None else _state()).get("lastAt")
    return not last or (now - datetime.fromisoformat(last)).total_seconds() >= EVERY_HOURS * 3600


# ---------------------------------------------------------------------------- encryption

def encrypt(data: bytes, passphrase: str) -> bytes:
    """AES-256-CBC with a PBKDF2 (SHA-256) key, in OpenSSL's "Salted__" file format."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    salt = os.urandom(8)
    key_iv = PBKDF2HMAC(hashes.SHA256(), 48, salt, PBKDF2_ITERATIONS).derive(passphrase.encode("utf-8"))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key_iv[:32]), modes.CBC(key_iv[32:])).encryptor()
    return b"Salted__" + salt + enc.update(padded) + enc.finalize()


def decrypt(blob: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    if blob[:8] != b"Salted__":
        raise ValueError("not an openssl-format file")
    salt = blob[8:16]
    key_iv = PBKDF2HMAC(hashes.SHA256(), 48, salt, PBKDF2_ITERATIONS).derive(passphrase.encode("utf-8"))
    dec = Cipher(algorithms.AES(key_iv[:32]), modes.CBC(key_iv[32:])).decryptor()
    padded = dec.update(blob[16:]) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ---------------------------------------------------------------------------- the store

def _load(name: str) -> dict:
    """Remembered rows of one table; {} when the copy is missing or damaged (cache.read_file never
    raises, and a damaged copy is moved aside and named in the step's stats)."""
    data = cache.read_file(tables_dir() / f"{name}.json.gz", dict, check=lambda d: all(isinstance(r, dict) for r in d.values()))
    return data if data is not None else {}


def _save(name: str, rows: dict) -> None:
    cache.write_file(tables_dir() / f"{name}.json.gz", rows, compresslevel=6, ensure_ascii=False)


def _state() -> dict:
    data = cache.read_file(store_dir() / "state.json", dict, gz=False)
    return data if data is not None else {}


def _save_state(st: dict) -> None:
    store_dir().mkdir(parents=True, exist_ok=True)
    (store_dir() / "state.json").write_text(json.dumps(st), encoding="utf-8")


def _row(*parts) -> dict:
    out: dict = {}
    for r in parts:
        if r is not None:
            out.update({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r._asdict().items()})
    for k in ("len_content_md", "len_embedding", "len_pulse", "sig", "has_discussion_url"):
        out.pop(k, None)
    return out


def collect(conn) -> dict[str, dict]:
    """Every table as {id: row}. Tracked tables: what the runner's copy shows now, merged over what
    it showed before (rows keep their last known version once they age out of the copy)."""
    tables: dict[str, dict] = {}
    # Text columns only for published rows: the export keeps those in the runner's copy every run, so
    # they cost no reads here. Other rows (rejected articles, unpublished stories) keep their small
    # columns (status, dates, scores) and whatever text an earlier backup already had.
    stories = cache.stories(conn)
    s_text = cache.story_text(conn, [s for s in stories.values() if s.status == "published"])
    tables["stories"] = _load("stories")
    for i, s in stories.items():
        tables["stories"][str(i)] = {**tables["stories"].get(str(i), {}), **_row(s, s_text.get(i))}
    articles = cache.articles(conn)
    a_text = cache.article_text(conn, [a for a in articles.values() if a.status == "published"])
    tables["articles"] = _load("articles")
    for i, a in articles.items():
        tables["articles"][str(i)] = {**tables["articles"].get(str(i), {}), **_row(a, a_text.get(i))}
    threads = cache.threads(conn)
    t_text = cache.thread_text(conn, [t for t in threads.values() if t.status == "published"])
    tables["threads"] = _load("threads")
    for i, t in threads.items():
        tables["threads"][str(i)] = {**tables["threads"].get(str(i), {}), **_row(t, t_text.get(i))}
    # Rows deleted by hand disappear from the mirror through deleted_rows; drop them here too.
    gone = conn.execute(select(db.deleted_rows.c.table_name, db.deleted_rows.c.row_id)).all()
    for table, rid in gone:
        tables.get(table, {}).pop(str(rid), None)
    for name in SMALL_TABLES:
        table = db.metadata.tables[name]
        key = "day" if name == "daily_stats" else "id"
        tables[name] = {str(r._mapping[key]): {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r._mapping.items()}
                        for r in conn.execute(select(table)).all()}
    return tables


def pack(tables: dict[str, dict], now: datetime) -> bytes:
    """tar.gz of <table>.jsonl plus a manifest with counts."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(now.timestamp())
            tar.addfile(info, io.BytesIO(data))

        counts = {}
        for name, rows in tables.items():
            lines = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows.values())
            add(f"{name}.jsonl", (lines + "\n").encode("utf-8") if lines else b"")
            counts[name] = len(rows)
        add("manifest.json", json.dumps({"format": FORMAT, "generatedAt": now.isoformat(), "tables": counts,
                                         "excluded": ["events", "push_subscriptions", "runs", "article text", "embeddings"]}, indent=1).encode("utf-8"))
    return buf.getvalue()


def unpack(data: bytes) -> dict[str, dict]:
    tables: dict[str, dict] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.name.endswith(".jsonl"):
                continue
            name = m.name[:-6]
            key = "day" if name == "daily_stats" else "id"
            rows = {}
            for line in (tar.extractfile(m).read().decode("utf-8")).splitlines():
                if line.strip():
                    r = json.loads(line)
                    rows[str(r.get(key))] = r
            tables[name] = rows
    return tables


# ---------------------------------------------------------------------------- restore
# The backup's tables go back into an empty database in this order (sources before the articles that
# point at them, stories before their articles). Rows come from the runner's copy (cache.py), so
# they carry its column names: the first eight characters of reject_reason are "reject_kind".
RESTORE_ORDER = ("sources", "threads", "stories", "articles", "topics", "newsletters", "social_posts", "llm_usage", "daily_stats")


def _restore_row(table, row: dict) -> dict | None:
    """One backup row as values for `table`, or None when a required column is missing."""
    from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, BigInteger

    row = dict(row)
    if table.name == "articles":
        if not row.get("reject_reason") and row.get("reject_kind"):
            row["reject_reason"] = row["reject_kind"]
        row.setdefault("fetched_at", row.get("created_at"))  # text columns are kept for published rows only
    out = {}
    for col in table.columns:
        if col.name == "rev" or col.name not in row:  # rev: the change-tracking trigger stamps it
            continue
        v = row[col.name]
        if v is not None:
            if isinstance(col.type, DateTime) and isinstance(v, str):
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            elif isinstance(col.type, Boolean):
                v = bool(v)
            elif isinstance(col.type, (Integer, BigInteger)) and not isinstance(v, bool):
                v = int(v)
            elif isinstance(col.type, Float):
                v = float(v)
            elif isinstance(col.type, JSON) and isinstance(v, str) and col.name == "embedding":
                pass  # a packed vector stays a string
        out[col.name] = v
    for col in table.columns:
        required = not col.nullable and not col.primary_key and col.default is None and col.server_default is None
        if required and out.get(col.name) is None:
            return None
    return out


def restore(tables: dict[str, dict], eng, force: bool = False) -> dict:
    """Load unpacked backup tables into the database behind `eng` (an empty one: the tables must hold
    no rows unless force). Returns rows restored and skipped per table. Rows that lack a required
    column (an article the backup only knew by its small columns: no address) are skipped, and an
    article whose story was skipped keeps no story link."""
    db.metadata.create_all(eng)
    out: dict = {"restored": {}, "skipped": {}}
    with eng.begin() as conn:
        if not force:
            for name in RESTORE_ORDER:
                if conn.execute(select(db.metadata.tables[name].c[ "day" if name == "daily_stats" else "id"]).limit(1)).first():
                    raise RuntimeError(f"table {name} is not empty: restore only into a new database")
        kept_ids: dict[str, set] = {}
        for name in RESTORE_ORDER:
            table = db.metadata.tables[name]
            rows, skipped = [], 0
            for r in (tables.get(name) or {}).values():
                v = _restore_row(table, r)
                if v is None:
                    skipped += 1
                    continue
                if name == "articles":
                    if v.get("story_id") is not None and v["story_id"] not in kept_ids.get("stories", set()):
                        v["story_id"] = None
                    if v.get("source_id") is not None and v["source_id"] not in kept_ids.get("sources", set()):
                        v["source_id"] = None
                rows.append(v)
            # Unique columns (slugs, addresses) keep their first row: a backup merged over several
            # nights can hold an old row and its renamed successor.
            for col in [c for c in table.columns if c.unique]:
                seen: set = set()
                kept = []
                for v in rows:
                    key = v.get(col.name)
                    if key is not None and key in seen:
                        skipped += 1
                        continue
                    seen.add(key)
                    kept.append(v)
                rows = kept
            for k in range(0, len(rows), 500):
                conn.execute(table.insert(), rows[k:k + 500])
            if "id" in table.c:
                kept_ids[name] = {v["id"] for v in rows if v.get("id") is not None}
            out["restored"][name] = len(rows)
            if skipped:
                out["skipped"][name] = skipped
        if eng.dialect.name == "postgresql":
            # Rows went in with their own ids: move each id sequence past them.
            for name in RESTORE_ORDER:
                if "id" in db.metadata.tables[name].c:
                    conn.execute(text(f"select setval(pg_get_serial_sequence('{name}', 'id'), coalesce((select max(id) from {name}), 0) + 1, false)"))
    return out


def verify(blob: bytes, passphrase: str | None) -> dict:
    """Open a backup (encrypted, or an already decrypted .tar.gz when passphrase is None) the way a
    restore would and load it into a throwaway in-memory SQLite database: proof the file restores,
    and the row counts it gives back."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    tables = unpack(decrypt(blob, passphrase) if passphrase is not None else blob)
    eng = create_engine("sqlite://", future=True, poolclass=StaticPool, connect_args={"check_same_thread": False})
    try:
        got = restore(tables, eng)
        with eng.connect() as conn:
            for name in ("stories", "articles"):
                got.setdefault("check", {})[name] = int(conn.execute(select(func.count()).select_from(db.metadata.tables[name])).scalar() or 0)
            # A published story the site can show: it has a slug, a headline and at least one article.
            s, a = db.stories.c, db.articles.c
            got["check"]["publishedWithArticles"] = int(conn.execute(
                select(func.count(func.distinct(s.id))).select_from(db.stories.join(db.articles, a.story_id == s.id))
                .where(s.status == "published")).scalar() or 0)
    finally:
        eng.dispose()
    return got


def restore_file(path: str, passphrase: str, force: bool = False) -> dict:
    """python -m digest.backup restore FILE: an encrypted backup into DATABASE_URL (a new database)."""
    blob = Path(path).read_bytes()
    tables = unpack(decrypt(blob, passphrase) if blob[:8] == b"Salted__" else blob)
    return restore(tables, db.engine(), force=force)


def seed(store: media.Store, passphrase: str) -> int:
    """A missing store starts from the newest backup asset, so older rows are not lost."""
    try:
        rel = store.release(RELEASE_TAG, "Database backups", "Nightly encrypted copies of the essential tables (pipeline/digest/backup.py). Not a software release.")
        assets = sorted((a for a in store.assets(rel["id"], pages=1) if NAME_RE.match(a.get("name") or "")), key=lambda a: a["name"])
        if not assets:
            return 0
        raw = store.download(assets[-1]["browser_download_url"])
        if not raw:
            return 0
        tables = unpack(decrypt(raw, passphrase))
        n = 0
        for name in TRACKED:
            if tables.get(name):
                _save(name, tables[name])
                n += len(tables[name])
        log.info("backup store seeded from %s: %d rows", assets[-1]["name"], n)
        return n
    except Exception as exc:  # noqa: BLE001 - seeding is best effort
        log.warning("could not seed the backup store: %s", str(exc)[:160])
        return 0


def rotate(store: media.Store, release_id: int, now: datetime) -> int:
    cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    n = 0
    for a in store.assets(release_id, pages=1):
        m = NAME_RE.match(a.get("name") or "")
        if m and m.group(1) < cutoff:
            store.delete_asset(a["id"])
            n += 1
    return n


def run(session=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"done": False, "reason": ""}
    passphrase = os.environ.get("BACKUP_KEY") or ""
    st = _state()
    force = os.environ.get("BACKUP_FORCE") == "1"
    marker().unlink(missing_ok=True)
    if not passphrase:
        stats["reason"] = "no BACKUP_KEY"
    elif not force and not due(now, st):
        stats["reason"] = f"last backup {st['lastAt'][:16]}"
    if stats["reason"]:
        _write_status(st, stats, now)
        return stats
    store = media.open_store(session)
    if store is None:
        stats["reason"] = "no token"
        _write_status(st, stats, now)
        return stats
    if not any((tables_dir() / f"{t}.json.gz").exists() for t in TRACKED):
        stats["seeded"] = seed(store, passphrase)
    b0 = db.bytes_read()
    with db.engine().connect() as conn:
        tables = collect(conn)
    stats["readKBOwn"] = round((db.bytes_read() - b0) / 1024, 1)
    for name in TRACKED:
        _save(name, tables[name])
    marker().parent.mkdir(parents=True, exist_ok=True)
    marker().write_text(now.isoformat(), encoding="utf-8")
    archive = pack(tables, now)
    blob = encrypt(archive, passphrase)
    name = f"digest-backup-{now.date().isoformat()}.tar.gz.enc"
    stats.update(rows={k: len(v) for k, v in tables.items()}, bytes=len(blob), mb=round(len(blob) / 1048576, 2), sha256=hashlib.sha256(blob).hexdigest()[:12])
    # Before it is uploaded, the file is opened and loaded into a throwaway database, as a restore
    # would: a backup that cannot be restored is reported, not trusted.
    try:
        checked = verify(blob, passphrase)
        stats["restoreCheck"] = {"ok": True, **checked.get("check", {}), "skipped": checked.get("skipped", {})}
    except Exception as exc:  # noqa: BLE001 - the upload still goes ahead; the failure is on the dashboard
        log.warning("backup restore check failed: %s", str(exc)[:200])
        stats["restoreCheck"] = {"ok": False, "error": str(exc)[:160]}
    try:
        rel = store.release(RELEASE_TAG, "Database backups", "Nightly encrypted copies of the essential tables (pipeline/digest/backup.py). Not a software release.")
        for a in store.assets(rel["id"], pages=1):
            if a.get("name") == name:  # the same day again (forced run): replace
                store.delete_asset(a["id"])
        store.upload(rel["id"], name, blob)
        stats["deleted"] = rotate(store, rel["id"], now)
        stats["done"] = True
        stats["asset"] = name
        st.update(lastAt=now.isoformat(), lastAsset=name, lastMB=stats["mb"], lastRows=stats["rows"],
                  lastRestoreCheck=stats["restoreCheck"])
    except Exception as exc:  # noqa: BLE001 - a failed upload is retried next run
        log.warning("backup upload failed: %s", str(exc)[:200])
        stats["reason"] = f"upload failed: {str(exc)[:100]}"
    st["calls"] = store.calls
    media.save_manifest()
    _write_status(st, stats, now)
    return stats


def _write_status(st: dict, stats: dict, now: datetime) -> None:
    st["checkedAt"] = now.isoformat()
    st["reason"] = stats.get("reason") or ""
    _save_state(st)


def main(argv: list[str]) -> int:
    """python -m digest.backup restore FILE [--force]: load a backup (encrypted with BACKUP_KEY, or an
    already decrypted .tar.gz) into the database DATABASE_URL names, which must be a new, empty one.
    python -m digest.backup verify FILE: the same into a throwaway in-memory database, counts only."""
    import sys

    if len(argv) < 2 or argv[0] not in ("restore", "verify"):
        print(main.__doc__)
        return 2
    passphrase = os.environ.get("BACKUP_KEY") or ""
    blob = Path(argv[1]).read_bytes()
    if blob[:8] == b"Salted__" and not passphrase:
        print("BACKUP_KEY is not set", file=sys.stderr)
        return 2
    if argv[0] == "verify":
        out = verify(blob, passphrase if blob[:8] == b"Salted__" else None)
    else:
        out = restore_file(argv[1], passphrase, force="--force" in argv)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
