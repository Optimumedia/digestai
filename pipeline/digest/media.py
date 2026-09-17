"""Durable home for generated media: share images, thumbnails and the spoken briefing.

They used to live in the site's Pages tree (1 GB limit, ~20 MB a day of growth) and survive
between runs only in the Actions cache (10 GB, a fresh 50 MB copy per run, evicted at will).
Now each file is uploaded once as an asset of a GitHub release (free, kept for good, served by
GitHub's CDN), one release per week under the tag media-<year>-W<week>, and the site links to
it: https://github.com/<repo>/releases/download/<tag>/<name>. Only new files are uploaded, a
bounded number per run.

Files wait in pipeline/data/cache/media/pending until uploaded (that directory rides in the
read cache the workflow keeps between runs). When the store cannot be reached, pending files
are copied into site/public/media so the site still serves them from Pages this run, and the
upload is tried again next run. The manifest of what is uploaded is kept next to the pending
files and, once a day, uploaded to the small "media-index" release, so a lost cache costs one
download rather than thousands of duplicate uploads.

Steps that make media call queue(); the `media` step (after images and audio) uploads.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger("digest.media")

FORMAT = 1
INDEX_TAG = "media-index"        # holds manifest.json only
MAX_UPLOADS_PER_RUN = 200
MAX_UPLOAD_MB_PER_RUN = 60
UPLOAD_TIME_BUDGET_SECONDS = 240
MANIFEST_UPLOAD_HOURS = 20       # the manifest goes to the index release at most this often
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
CONTENT_TYPES = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".mp3": "audio/mpeg", ".json": "application/json"}

# Overridden by tests.
SITE_PUBLIC = config.ROOT / "site" / "public"


def store_dir() -> Path:
    return config.CACHE_DIR / "media"


def pending_dir() -> Path:
    return store_dir() / "pending"


def fallback_dir() -> Path:
    """Pages directory for files the store has not taken yet (served as /media/<name>)."""
    return SITE_PUBLIC / "media"


def repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "Optimumedia/digestai"


def release_tag(now: datetime | None = None) -> str:
    y, w, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f"media-{y}-W{w:02d}"


def asset_url(tag: str, name: str, repository: str | None = None) -> str:
    return f"https://github.com/{repository or repo()}/releases/download/{tag}/{name}"


# ---------------------------------------------------------------------------- manifest

_manifest: dict | None = None


def _empty() -> dict:
    return {"format": FORMAT, "repo": repo(), "assets": {}, "skipped": {}, "releases": {}, "manifestUploadedAt": None, "lastUploadAt": None}


def manifest() -> dict:
    global _manifest
    if _manifest is None:
        path = store_dir() / "manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _manifest = data if data.get("format") == FORMAT and isinstance(data.get("assets"), dict) else _empty()
        except (OSError, ValueError):
            _manifest = _empty()
        for key, default in _empty().items():
            _manifest.setdefault(key, default)
    return _manifest


def save_manifest() -> None:
    if _manifest is None:
        return
    path = store_dir() / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_manifest, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def reset() -> None:
    """Forget the loaded manifest (tests)."""
    global _manifest
    _manifest = None


# ---------------------------------------------------------------------------- what other steps ask

def queue(name: str, data: bytes) -> Path:
    """Hand a finished file to the store; it is uploaded by the next `media` step."""
    path = pending_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def pending_path(name: str) -> Path:
    return pending_dir() / name


def uploaded(name: str) -> bool:
    return name in manifest()["assets"]


def has(name: str) -> bool:
    """Uploaded or waiting to be."""
    return uploaded(name) or pending_path(name).exists()


def url(name: str) -> str | None:
    """Where the site can load this file from: the release, or Pages while it waits. None if unknown."""
    entry = manifest()["assets"].get(name)
    if entry:
        return asset_url(entry[0], name, manifest().get("repo"))
    if pending_path(name).exists() or (fallback_dir() / name).exists():
        return f"{config.SITE_URL}/media/{name}"
    return None


def local_path(name: str) -> Path | None:
    """A copy on this runner, if there is one."""
    for p in (pending_path(name), fallback_dir() / name):
        if p.exists():
            return p
    return None


def content(name: str, fetch=None) -> bytes | None:
    """The file's bytes: from this runner when present, else downloaded from the store."""
    p = local_path(name)
    if p:
        return p.read_bytes()
    u = url(name)
    if not u or not u.startswith("https://github.com/"):
        return None
    try:
        r = (fetch or requests.get)(u, timeout=30)
        return r.content if r.status_code == 200 else None
    except requests.RequestException as exc:
        log.info("could not fetch %s: %s", name, str(exc)[:80])
        return None


def skipped_recently(name: str, hours: float = 24) -> bool:
    entry = manifest()["skipped"].get(name)
    if not entry:
        return False
    if entry.get("tries", 1) >= 3:
        return True
    return time.time() - float(entry.get("at") or 0) < hours * 3600


def mark_skipped(name: str, reason: str) -> None:
    entry = manifest()["skipped"].get(name) or {"tries": 0}
    manifest()["skipped"][name] = {"at": time.time(), "tries": entry.get("tries", 0) + 1, "reason": reason[:80]}


# ---------------------------------------------------------------------------- the store

class Store:
    """The GitHub releases API for one repository. `session` is any object with get/post/delete
    returning responses with status_code, json() and content (tests pass a fake)."""

    def __init__(self, token: str, repository: str, session=None):
        self.repo = repository
        self.calls = 0
        if session is None:
            session = requests.Session()
            session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                    "X-GitHub-Api-Version": "2022-11-28"})
        self.s = session

    def _call(self, method: str, url: str, **kw):
        self.calls += 1
        kw.setdefault("timeout", 60)
        return getattr(self.s, method)(url, **kw)

    def release(self, tag: str, title: str, body: str) -> dict:
        """The release with this tag (created when missing). Never becomes the repo's 'latest'."""
        known = manifest()["releases"].get(tag)
        if known:
            return known
        r = self._call("get", f"{API}/repos/{self.repo}/releases/tags/{tag}")
        if r.status_code == 404:
            r = self._call("post", f"{API}/repos/{self.repo}/releases",
                           json={"tag_name": tag, "name": title, "body": body, "prerelease": False, "make_latest": "false"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"release {tag}: HTTP {r.status_code} {str(r.text)[:120]}")
        data = r.json()
        entry = {"id": data["id"]}
        manifest()["releases"][tag] = entry
        return entry

    def upload(self, release_id: int, name: str, data: bytes) -> bool:
        """True when the asset is in the release afterwards (uploaded now, or already there)."""
        ctype = CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        r = self._call("post", f"{UPLOADS}/repos/{self.repo}/releases/{release_id}/assets", params={"name": name},
                       data=data, headers={"Content-Type": ctype}, timeout=120)
        if r.status_code == 201:
            return True
        if r.status_code == 422 and "already_exists" in str(r.text):
            return True
        raise RuntimeError(f"upload {name}: HTTP {r.status_code} {str(r.text)[:120]}")

    def assets(self, release_id: int, pages: int = 3) -> list[dict]:
        out = []
        for page in range(1, pages + 1):
            r = self._call("get", f"{API}/repos/{self.repo}/releases/{release_id}/assets", params={"per_page": 100, "page": page})
            if r.status_code != 200:
                break
            batch = r.json()
            out += batch
            if len(batch) < 100:
                break
        return out

    def delete_asset(self, asset_id: int) -> None:
        self._call("delete", f"{API}/repos/{self.repo}/releases/assets/{asset_id}")

    def download(self, u: str) -> bytes | None:
        r = self._call("get", u, headers={"Accept": "application/octet-stream"}, timeout=120)
        return r.content if r.status_code == 200 else None


def open_store(session=None) -> Store | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if session is None and not (token and repository):
        return None
    return Store(token or "", repository or repo(), session)


def recover_manifest(store: Store) -> bool:
    """A missing manifest (the cache was evicted) is fetched back from the index release."""
    try:
        rel = store.release(INDEX_TAG, "Media index", "The manifest of the media store (pipeline/digest/media.py). Not a software release.")
        for a in store.assets(rel["id"], pages=1):
            if a.get("name") == "manifest.json":
                raw = store.download(a["browser_download_url"])
                data = json.loads(raw.decode("utf-8")) if raw else None
                if data and data.get("format") == FORMAT and isinstance(data.get("assets"), dict):
                    m = manifest()
                    for k, v in data["assets"].items():
                        m["assets"].setdefault(k, v)
                    m["releases"].setdefault(INDEX_TAG, rel)
                    log.info("recovered the media manifest: %d assets", len(data["assets"]))
                    return True
    except Exception as exc:  # noqa: BLE001 - recovery is best effort
        log.warning("could not recover the media manifest: %s", str(exc)[:120])
    return False


def upload_manifest(store: Store, now: datetime) -> bool:
    m = manifest()
    last = m.get("manifestUploadedAt")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < MANIFEST_UPLOAD_HOURS * 3600:
        return False
    rel = store.release(INDEX_TAG, "Media index", "The manifest of the media store (pipeline/digest/media.py). Not a software release.")
    for a in store.assets(rel["id"], pages=1):
        if a.get("name") == "manifest.json":
            store.delete_asset(a["id"])
    body = json.dumps({k: v for k, v in m.items() if k in ("format", "repo", "assets")}, separators=(",", ":")).encode("utf-8")
    store.upload(rel["id"], "manifest.json", body)
    m["manifestUploadedAt"] = now.isoformat()
    return True


# ---------------------------------------------------------------------------- the step

def _site_index() -> dict:
    """site/src/data/media.json: where each story's share image and thumbnail, and each episode, is.
    Values are a release tag (the site composes the URL) or a Pages path for files still waiting."""
    m = manifest()
    stories_file = config.SITE_DATA_DIR / "stories.json"
    slugs = []
    if stories_file.exists():
        try:
            slugs = [s["slug"] for s in json.loads(stories_file.read_text(encoding="utf-8"))]
        except (ValueError, KeyError, TypeError):
            slugs = []
    fallback = fallback_dir()

    def where(name: str) -> str | None:
        entry = m["assets"].get(name)
        if entry:
            return entry[0]
        return f"/media/{name}" if (fallback / name).exists() else None

    og = {s: where(f"og-{s}.png") for s in slugs}
    thumb = {s: where(f"thumb-{s}.webp") for s in slugs}
    return {
        "repo": m.get("repo") or repo(),
        "og": {k: v for k, v in og.items() if v},
        "thumb": {k: v for k, v in thumb.items() if v},
    }


def write_site_index() -> dict:
    index = _site_index()
    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / "media.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    return index


def _status(stats: dict, now: datetime) -> None:
    m = manifest()
    total = sum(int(v[1] or 0) for v in m["assets"].values())
    status = {"at": now.isoformat(), "assets": len(m["assets"]), "mb": round(total / 1048576, 1),
              "pending": stats.get("pending", 0), "store": stats.get("store"), "lastUploadAt": m.get("lastUploadAt"),
              "uploaded": stats.get("uploaded", 0), "error": stats.get("error")}
    path = store_dir() / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status), encoding="utf-8")


def _copy_fallback(files: list[Path]) -> int:
    """Files the store did not take are served from Pages this run."""
    n = 0
    for p in files:
        try:
            fallback_dir().mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, fallback_dir() / p.name)
            n += 1
        except OSError as exc:
            log.warning("could not copy %s to the site: %s", p.name, exc)
    return n


def run(session=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"uploaded": 0, "uploadedMB": 0.0, "pending": 0, "fallback": 0, "store": "ok", "calls": 0}
    pending_dir().mkdir(parents=True, exist_ok=True)
    # Whatever was copied to Pages by an earlier run is not needed again: it is either uploaded now or
    # copied again below.
    if fallback_dir().exists():
        shutil.rmtree(fallback_dir(), ignore_errors=True)
    store = open_store(session)
    if store is None:
        stats["store"] = "no token"
    elif not manifest()["assets"] and not manifest().get("manifestUploadedAt"):
        recover_manifest(store)
    # Oldest first, so a backlog clears in order; a file the store already has is simply dropped.
    files = sorted((p for p in pending_dir().iterdir() if p.is_file()), key=lambda p: (p.stat().st_mtime, p.name))
    for p in [p for p in files if uploaded(p.name)]:
        p.unlink(missing_ok=True)
    files = [p for p in files if not uploaded(p.name)]
    if store is not None:
        t0 = time.time()
        budget = MAX_UPLOAD_MB_PER_RUN * 1048576
        try:
            tag = release_tag(now)
            rel = None
            for p in list(files):
                if stats["uploaded"] >= MAX_UPLOADS_PER_RUN or time.time() - t0 > UPLOAD_TIME_BUDGET_SECONDS:
                    break
                data = p.read_bytes()
                if stats["uploaded"] and budget - len(data) < 0:
                    break
                if rel is None:
                    rel = store.release(tag, f"Media {tag[6:]}", "Share images, thumbnails and spoken briefings for digestai.news, "
                                        "uploaded by the pipeline (pipeline/digest/media.py). Not a software release.")
                if store.upload(rel["id"], p.name, data):
                    manifest()["assets"][p.name] = [tag, len(data), now.isoformat()]
                    manifest()["lastUploadAt"] = now.isoformat()
                    stats["uploaded"] += 1
                    budget -= len(data)
                    stats["uploadedMB"] = round(stats["uploadedMB"] + len(data) / 1048576, 2)
                    files.remove(p)
                    p.unlink(missing_ok=True)
            if stats["uploaded"] or not manifest().get("manifestUploadedAt"):
                if upload_manifest(store, now):
                    stats["manifest"] = "uploaded"
        except Exception as exc:  # noqa: BLE001 - the store being down must not stop the site
            log.warning("media store: %s", str(exc)[:200])
            stats["store"] = "unreachable"
            stats["error"] = str(exc)[:160]
        stats["calls"] = store.calls
    stats["pending"] = len(files)
    stats["fallback"] = _copy_fallback(files)
    save_manifest()
    stats["assets"] = len(manifest()["assets"])
    write_site_index()
    try:
        from . import audio

        audio.refresh_urls()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not refresh episode links: %s", str(exc)[:120])
    _status(stats, now)
    return stats
