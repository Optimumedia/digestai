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
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

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
    return config.CACHE_DIR / "backup"


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
    p = store_dir() / f"{name}.json.gz"
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, EOFError):
        pass
    return {}


def _save(name: str, rows: dict) -> None:
    store_dir().mkdir(parents=True, exist_ok=True)
    p = store_dir() / f"{name}.json.gz"
    tmp = p.with_name(p.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"), default=str)
    tmp.replace(p)


def _state() -> dict:
    try:
        return json.loads((store_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
    stories = cache.stories(conn)
    s_text = cache.story_text(conn, list(stories.values()))
    tables["stories"] = _load("stories")
    for i, s in stories.items():
        tables["stories"][str(i)] = _row(s, s_text.get(i))
    articles = cache.articles(conn)
    a_text = cache.article_text(conn, list(articles.values()))
    tables["articles"] = _load("articles")
    for i, a in articles.items():
        tables["articles"][str(i)] = _row(a, a_text.get(i))
    threads = cache.threads(conn)
    t_text = cache.thread_text(conn, list(threads.values()))
    tables["threads"] = _load("threads")
    for i, t in threads.items():
        tables["threads"][str(i)] = _row(t, t_text.get(i))
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
    last = st.get("lastAt")
    if not passphrase:
        stats["reason"] = "no BACKUP_KEY"
    elif last and not force and (now - datetime.fromisoformat(last)).total_seconds() < EVERY_HOURS * 3600:
        stats["reason"] = f"last backup {last[:16]}"
    if stats["reason"]:
        _write_status(st, stats, now)
        return stats
    store = media.open_store(session)
    if store is None:
        stats["reason"] = "no token"
        _write_status(st, stats, now)
        return stats
    if not any((store_dir() / f"{t}.json.gz").exists() for t in TRACKED):
        stats["seeded"] = seed(store, passphrase)
    b0 = db.bytes_read()
    with db.engine().connect() as conn:
        tables = collect(conn)
    stats["readKBOwn"] = round((db.bytes_read() - b0) / 1024, 1)
    for name in TRACKED:
        _save(name, tables[name])
    archive = pack(tables, now)
    blob = encrypt(archive, passphrase)
    name = f"digest-backup-{now.date().isoformat()}.tar.gz.enc"
    stats.update(rows={k: len(v) for k, v in tables.items()}, bytes=len(blob), mb=round(len(blob) / 1048576, 2), sha256=hashlib.sha256(blob).hexdigest()[:12])
    try:
        rel = store.release(RELEASE_TAG, "Database backups", "Nightly encrypted copies of the essential tables (pipeline/digest/backup.py). Not a software release.")
        for a in store.assets(rel["id"], pages=1):
            if a.get("name") == name:  # the same day again (forced run): replace
                store.delete_asset(a["id"])
        store.upload(rel["id"], name, blob)
        stats["deleted"] = rotate(store, rel["id"], now)
        stats["done"] = True
        stats["asset"] = name
        st.update(lastAt=now.isoformat(), lastAsset=name, lastMB=stats["mb"], lastRows=stats["rows"])
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
