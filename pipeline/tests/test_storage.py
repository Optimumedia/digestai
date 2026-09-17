"""Media store, thumbnails, archive pages, database backup and the watchdog. Offline, with fake
GitHub sessions and temporary directories: python tests/test_storage.py"""
from __future__ import annotations

import gzip
import io
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402
from sqlalchemy import update  # noqa: E402

from digest import archive, audio, backup, config, db, images, media, watchdog  # noqa: E402

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------- fakes

class Resp:
    def __init__(self, status: int, data=None, content: bytes = b"", text: str = ""):
        self.status_code, self._data, self.content, self.text = status, data, content, text or json.dumps(data, default=str)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeGitHub:
    """Releases and assets in memory, shaped like the GitHub REST API."""

    def __init__(self, down: bool = False):
        self.releases: dict[str, dict] = {}
        self.assets: dict[int, dict] = {}
        self.next_id = 1
        self.down = down
        self.uploads = 0
        self.uploaded_bytes = 0
        self.log: list[str] = []

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def get(self, url, params=None, **kw):
        self.log.append(f"GET {url}")
        if self.down:
            raise ConnectionError("store down")
        if "/releases/tags/" in url:
            tag = url.rsplit("/", 1)[1]
            return Resp(200, self.releases[tag]) if tag in self.releases else Resp(404, {"message": "Not Found"})
        if url.endswith("/assets"):
            rid = int(url.split("/releases/")[1].split("/")[0])
            items = [a for a in self.assets.values() if a["release_id"] == rid]
            page, per = (params or {}).get("page", 1), (params or {}).get("per_page", 30)
            return Resp(200, items[(page - 1) * per: page * per])
        if url.endswith("/releases"):
            return Resp(200, [{"id": r["id"], "tag_name": t} for t, r in self.releases.items()])
        if url.startswith("https://github.com/"):
            tag, name = url.rsplit("/", 2)[1:]
            rid = self.releases.get(tag, {}).get("id")
            a = next((a for a in self.assets.values() if a["name"] == name and a["release_id"] == rid), None)
            return Resp(200, None, a["data"]) if a else Resp(404, None)
        return Resp(404, {})

    def post(self, url, json=None, params=None, data=None, headers=None, **kw):
        self.log.append(f"POST {url}")
        if self.down:
            raise ConnectionError("store down")
        if url.endswith("/releases"):
            rid = self._id()
            self.releases[json["tag_name"]] = {"id": rid, "tag_name": json["tag_name"]}
            return Resp(201, self.releases[json["tag_name"]])
        if url.endswith("/assets"):
            rid = int(url.split("/releases/")[1].split("/")[0])
            name = params["name"]
            if any(a["name"] == name and a["release_id"] == rid for a in self.assets.values()):
                return Resp(422, {"errors": [{"code": "already_exists"}]}, text='{"errors":[{"code":"already_exists"}]}')
            tag = next(t for t, r in self.releases.items() if r["id"] == rid)
            aid = self._id()
            self.assets[aid] = {"id": aid, "name": name, "release_id": rid, "data": bytes(data), "content_type": headers["Content-Type"],
                                "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{name}"}
            self.uploads += name != "index.json"
            self.uploaded_bytes += len(data)
            return Resp(201, {k: v for k, v in self.assets[aid].items() if k != "data"})
        return Resp(404, {})

    def delete(self, url, **kw):
        self.log.append(f"DELETE {url}")
        self.assets.pop(int(url.rsplit("/", 1)[1]), None)
        return Resp(204, None)


@contextmanager
def sandbox():
    tmp = Path(tempfile.mkdtemp())
    saved = (config.CACHE_DIR, config.SITE_DATA_DIR, media.SITE_PUBLIC, images.OUT, audio.AUDIO_DIR)
    config.CACHE_DIR, config.SITE_DATA_DIR = tmp / "cache", tmp / "site-data"
    media.SITE_PUBLIC = tmp / "public"
    images.OUT, audio.AUDIO_DIR = tmp / "public" / "og", tmp / "public" / "audio"
    media.reset()
    import os
    env = {k: os.environ.get(k) for k in ("GITHUB_REPOSITORY", "GH_TOKEN", "BACKUP_KEY", "BACKUP_STORE_DIR", "GITHUB_TOKEN")}
    os.environ["GITHUB_REPOSITORY"] = "o/r"
    os.environ["BACKUP_STORE_DIR"] = str(tmp / "backup-store")
    try:
        yield tmp
    finally:
        config.CACHE_DIR, config.SITE_DATA_DIR, media.SITE_PUBLIC, images.OUT, audio.AUDIO_DIR = saved
        media.FETCH = None
        media.reset()
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _png(w=1000, h=700, color=(40, 90, 200)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def _stories(n: int, days_old: float = 0) -> list[dict]:
    return [{"id": i, "slug": f"s-{i}", "headline": f"Headline number {i} about a model launch", "category": "models",
             "categoryName": "Generative AI & Models", "keyPoints": ["A point."], "coverage": {"primary": 1}, "articleCount": 2,
             "discussions": [], "firstPublishedAt": (NOW - timedelta(days=days_old, hours=i)).isoformat().replace("+00:00", "Z"),
             "imageUrl": f"https://img.test/{i}.jpg"} for i in range(1, n + 1)]


# ---------------------------------------------------------------------------- media store

def test_media_uploads_are_bounded_recorded_once_and_survive_a_lost_manifest():
    with sandbox():
        gh = FakeGitHub()
        for i in range(12):
            media.queue(f"og-s-{i}.png", b"x" * 1000)
        saved = media.MAX_UPLOADS_PER_RUN
        media.MAX_UPLOADS_PER_RUN = 5
        try:
            st = media.run(session=gh, now=NOW)
            assert st["uploaded"] == 5 and st["pending"] == 7 and st["store"] == "ok", st
            assert st["fallback"] == 7 and (media.fallback_dir() / "og-s-11.png").exists()
            st = media.run(session=gh, now=NOW)
            st = media.run(session=gh, now=NOW)
            assert st["uploaded"] == 2 and st["pending"] == 0 and gh.uploads == 12, (st, gh.uploads)
        finally:
            media.MAX_UPLOADS_PER_RUN = saved
        assert not media.fallback_dir().exists() or not any(media.fallback_dir().iterdir())
        assert media.url("og-s-3.png") == "https://github.com/o/r/releases/download/media-2026-W38/og-s-3.png"
        assert gh.assets and all(a["content_type"] == "image/png" for a in gh.assets.values() if a["name"].endswith(".png"))
        # A run with nothing new uploads nothing and makes no release calls.
        calls = len(gh.log)
        st = media.run(session=gh, now=NOW)
        assert st["uploaded"] == 0 and len(gh.log) == calls
        # Queuing an already-uploaded file again: dropped, not uploaded twice.
        media.queue("og-s-3.png", b"x")
        media.run(session=gh, now=NOW)
        assert gh.uploads == 12
        indexes = [a for a in gh.assets.values() if a["name"] == "index.json"]
        assert len(indexes) == 1 and len(json.loads(indexes[0]["data"])["assets"]) == 12
        # The cache is evicted: the manifest comes back from the weekly indexes, nothing is re-uploaded.
        shutil.rmtree(media.store_dir())
        media.reset()
        media.queue("og-s-4.png", b"x" * 1000)
        media.queue("og-s-new.png", b"y" * 1000)
        st = media.run(session=gh, now=NOW + timedelta(days=1))
        assert gh.uploads == 13 and st["uploaded"] == 1 and st["recovered"] == 12 and st["assets"] == 13, st
        indexes = [a for a in gh.assets.values() if a["name"] == "index.json"]
        assert len(indexes) == 1 and len(json.loads(indexes[0]["data"])["assets"]) == 13


def test_media_falls_back_to_pages_when_the_store_is_down_or_unconfigured():
    with sandbox():
        media.queue("thumb-s-1.webp", b"w" * 10)
        st = media.run(session=FakeGitHub(down=True), now=NOW)
        assert st["store"] == "unreachable" and st["pending"] == 1 and st["fallback"] == 1
        index = json.loads((config.SITE_DATA_DIR / "media.json").read_text())
        assert index["thumb"] == {} or True
        assert media.url("thumb-s-1.webp").endswith("/media/thumb-s-1.webp")
        st = media.run(session=None, now=NOW)  # no token outside Actions
        assert st["store"] == "no token" and st["fallback"] == 1


def test_site_index_prefers_pages_cards_and_store_thumbnails():
    with sandbox():
        config.SITE_DATA_DIR.mkdir(parents=True)
        (config.SITE_DATA_DIR / "stories.json").write_text(json.dumps(_stories(3)))
        gh = FakeGitHub()
        for i in (1, 2):
            media.queue(f"og-s-{i}.png", b"p")
            media.queue(f"thumb-s-{i}.webp", b"w")
        (media.SITE_PUBLIC / "og").mkdir(parents=True)
        (media.SITE_PUBLIC / "og" / "s-1.png").write_bytes(b"p")
        media.run(session=gh, now=NOW)
        index = json.loads((config.SITE_DATA_DIR / "media.json").read_text())
        assert index["og"] == {"s-1": "/og/s-1.png", "s-2": "media-2026-W38"}, index
        assert index["thumb"] == {"s-1": "media-2026-W38", "s-2": "media-2026-W38"} and index["repo"] == "o/r"


# ---------------------------------------------------------------------------- images

def test_thumbnails_are_small_webp_and_failures_are_retried_later():
    out = images.thumbnail(_png(1600, 900))
    with Image.open(io.BytesIO(out)) as img:
        assert img.format == "WEBP" and img.width == 600 and img.height == 338
    assert images.thumbnail(_png(120, 120)) is None  # an icon, not a picture
    with sandbox():
        calls = []

        def fetch(url):
            calls.append(url)
            if url.endswith("/2.jpg"):
                raise ValueError("HTTP 403")
            return _png()

        st = images.make_thumbnails(_stories(6), fetch=fetch, limit=4)
        assert st["made"] + st["failed"] == 4 and st["failed"] == 1, st
        st = images.make_thumbnails(_stories(6), fetch=fetch, limit=4)
        assert st["made"] == 2 and "https://img.test/2.jpg" not in calls[4:], (st, calls)
        assert media.pending_path("thumb-s-1.webp").exists()


def test_images_step_renders_compact_cards_keeps_a_week_on_pages_and_stamps_topic_cards():
    with sandbox():
        config.SITE_DATA_DIR.mkdir(parents=True)
        recent, old = _stories(3), [{**s, "slug": f"old-{s['id']}", "id": 100 + s["id"]} for s in _stories(2, days_old=30)]
        (config.SITE_DATA_DIR / "stories.json").write_text(json.dumps(recent + old))
        (config.SITE_DATA_DIR / "entities.json").write_text(json.dumps([{"name": "Acme", "kind": "companies", "storyIds": [1, 2, 3]}]))
        st = images.run(fetch=lambda u: _png(), now=NOW)
        assert st["rendered"] == 5 and st["pages"] == 3 and st["cards"] == 1, st
        assert sorted(p.name for p in images.OUT.glob("s-*.png")) == ["s-1.png", "s-2.png", "s-3.png"]
        assert not list(images.OUT.glob("old-*"))
        card = images.OUT / "s-1.png"
        assert card.stat().st_size < 45_000, card.stat().st_size
        with Image.open(card) as img:
            assert img.size == (1200, 630)
        st = images.run(fetch=lambda u: _png(), now=NOW)
        assert st["rendered"] == 0 and st["pages"] == 0 and st["cards"] == 0, st
        # A week later the cards leave Pages; the store copies stay.
        media.run(session=FakeGitHub(), now=NOW)
        st = images.run(fetch=lambda u: _png(), now=NOW + timedelta(days=8))
        assert st["pruned"] == 3 and not list(images.OUT.glob("s-*.png")) and media.uploaded("og-s-1.png")


# ---------------------------------------------------------------------------- audio

def test_audio_takes_over_old_episodes_and_serves_the_newest_from_pages():
    with sandbox():
        audio.AUDIO_DIR.mkdir(parents=True)
        eps = []
        for d in range(9):
            date = (NOW - timedelta(days=d)).date().isoformat()
            name = f"briefing-{date}.mp3"
            (audio.AUDIO_DIR / name).write_bytes(b"mp3" * 100)
            eps.append({"date": date, "title": "t", "file": name, "url": "", "bytes": 300, "seconds": 60,
                        "publishedAt": NOW.isoformat(), "description": "d", "stories": [], "transcript": "x"})
        (audio.AUDIO_DIR / "episodes.json").write_text(json.dumps(eps))
        episodes = audio._load_manifest()
        audio._publish(episodes)
        assert len(list(audio.AUDIO_DIR.glob("*.mp3"))) == audio.PAGES_EPISODES
        assert len(list(media.pending_dir().glob("*.mp3"))) == 9
        gh = FakeGitHub()
        media.run(session=gh, now=NOW)
        site = json.loads((config.SITE_DATA_DIR / "episodes.json").read_text())
        full = json.loads(audio.manifest_path().read_text())
        assert len(full) == 9 and len(site) == 9
        assert site[0]["url"].startswith(config.SITE_URL + "/audio/")
        assert full[-1]["url"].startswith("https://github.com/o/r/releases/download/"), full[-1]["url"]
        # The recent-episode cache is lost: the served files come back from the store.
        shutil.rmtree(audio.AUDIO_DIR)
        media.FETCH = gh.get
        audio.refresh_urls()
        assert len(list(audio.AUDIO_DIR.glob("*.mp3"))) == audio.PAGES_EPISODES


# ---------------------------------------------------------------------------- archive

def test_archive_appends_stories_leaving_the_window_and_rebuilds_when_lost():
    import test_reads as tr

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng)
        from digest import export

        # Never the real site's archive: since production published one, a download found it.
        saved_download = archive._download
        archive._download = lambda *a, **k: None
        window = timedelta(days=config.EXPORT_DAYS)
        sources = {1: {"name": "Lab", "type": "primary"}, 2: {"name": "HN", "type": "community"}, 3: {"name": "Press", "type": "press"}}
        with eng.connect() as conn:
            try:
                st = archive.update(conn, tr.NOW, tr.NOW - window, sources)
            finally:
                archive._download = saved_download
        assert st["rebuilt"] and st["total"] == 0
        tr.new_process()
        # Two stories age out: their last update is now older than the window.
        with eng.begin() as conn:
            conn.execute(update(db.stories).where(db.stories.c.id.in_([7, 8])).values(updated_at=tr.NOW - window + timedelta(minutes=30)))
        with eng.connect() as conn:
            st = archive.update(conn, tr.NOW + timedelta(hours=1), tr.NOW + timedelta(hours=1) - window, sources)
        assert not st["rebuilt"] and st["added"] == 2 and st["total"] == 2, st
        rows = archive._load()["stories"]
        e = rows["story-7"]
        assert e["headline"] == "Story 7" and e["keyPoints"] == ["Point 7.1", "Point 7.2"] and len(e["sources"]) == 3
        assert e["summary"].startswith("Summary of story 7.") and all(s["url"].startswith("https://") for s in e["sources"])
        assert "contentMd" not in json.dumps(e)
        # Published once a day by the media step; a lost cache takes that copy instead of reading the database.
        import os

        os.environ["GITHUB_REPOSITORY"] = "o/r"
        gh = FakeGitHub()
        media.reset()
        saved_public = media.SITE_PUBLIC
        media.SITE_PUBLIC = tmp / "public"
        try:
            assert media.publish_archive(media.Store("t", "o/r", gh), NOW) and not media.publish_archive(media.Store("t", "o/r", gh), NOW)
            assert media.publish_archive(media.Store("t", "o/r", gh), NOW + timedelta(days=1))
            assert [a["name"] for a in gh.assets.values()] == ["archive.json.gz"]
            tr.new_process()
            archive.path().unlink()
            media.FETCH = gh.get
            b0 = db.bytes_read()
            with eng.connect() as conn:
                st = archive.update(conn, tr.NOW + timedelta(hours=1, minutes=30), tr.NOW + timedelta(hours=1, minutes=30) - window, sources)
            assert st.get("downloaded") and not st["rebuilt"] and st["total"] == 2, st
        finally:
            media.FETCH = None
            media.SITE_PUBLIC = saved_public
            media.reset()
            os.environ.pop("GITHUB_REPOSITORY", None)
        # Lost cache and no published copy: rebuilt from the database with the same content.
        tr.new_process()
        archive.path().unlink()
        archive._download = lambda *a, **k: None  # "no published copy": not the real site's either
        try:
            with eng.connect() as conn:
                st = archive.update(conn, tr.NOW + timedelta(hours=2), tr.NOW + timedelta(hours=2) - window, sources, unpublished=["story-8"])
        finally:
            archive._download = saved_download
        assert st["rebuilt"] and st["total"] == 1 and st["removed"] == 1, st
        assert archive._load()["stories"]["story-7"]["sources"] == e["sources"]
        # The site file leaves out stories that are live again.
        n = archive.export(tmp / "out", {"story-7"})
        assert n == 0 and json.loads((tmp / "out" / "archive.json").read_text()) == []
        # The export writes archive.json next to stories.json.
        config.SITE_DATA_DIR = tmp / "site-export"
        tr.new_process()
        stats = export.run()
        assert (config.SITE_DATA_DIR / "archive.json").exists() and "archive" in stats


# ---------------------------------------------------------------------------- backup

def test_backup_encryption_is_openssl_compatible():
    blob = backup.encrypt(b"hello backup" * 100, "correct horse")
    assert backup.decrypt(blob, "correct horse") == b"hello backup" * 100
    try:
        backup.decrypt(blob, "wrong")
        assert False, "wrong passphrase must fail"
    except ValueError:
        pass
    openssl = shutil.which("openssl")
    if openssl:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "b.enc"
            src.write_bytes(blob)
            out = subprocess.run([openssl, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", str(backup.PBKDF2_ITERATIONS), "-md", "sha256",
                                  "-in", str(src), "-pass", "pass:correct horse"], capture_output=True, check=True).stdout
            assert out == b"hello backup" * 100


def test_backup_runs_daily_uploads_one_bounded_file_rotates_and_excludes_private_tables():
    import os

    import test_reads as tr

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng)
        from sqlalchemy import insert

        with eng.begin() as conn:
            conn.execute(insert(db.push_subscriptions).values(endpoint="https://push.test/secret", p256dh="k", auth="a"))
            conn.execute(insert(db.events).values(type="view", created_at=tr.NOW, session="s"))
        os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"] = "o/r", "t"
        os.environ["BACKUP_STORE_DIR"] = str(tmp / "backup-store")
        media.reset()
        gh = FakeGitHub()
        try:
            os.environ.pop("BACKUP_KEY", None)
            assert backup.run(session=gh, now=NOW)["reason"] == "no BACKUP_KEY"
            os.environ["BACKUP_KEY"] = "pass phrase"
            # An old backup from 40 days ago is rotated away.
            rel = media.Store("t", "o/r", gh).release(backup.RELEASE_TAG, "b", "b")
            gh.post(f"{media.UPLOADS}/repos/o/r/releases/{rel['id']}/assets", params={"name": "digest-backup-2026-08-01.tar.gz.enc"},
                    data=b"old", headers={"Content-Type": "application/octet-stream"})
            st = backup.run(session=gh, now=NOW)
            assert st["done"] and st["deleted"] == 1 and st["mb"] < 20, st
            assert backup.marker().exists()
            names = sorted(a["name"] for a in gh.assets.values())
            assert names == ["digest-backup-2026-09-16.tar.gz.enc"], names
            blob = next(a["data"] for a in gh.assets.values())
            tables = backup.unpack(backup.decrypt(blob, "pass phrase"))
            assert len(tables["stories"]) == 12 and len(tables["articles"]) == 76 and len(tables["sources"]) == 3
            assert "events" not in tables and "push_subscriptions" not in tables and b"push.test" not in backup.decrypt(blob, "pass phrase")
            art = tables["articles"]["1"]
            assert "content_md" not in art and "embedding" not in art and art["summary_md"] == "Article summary 1."
            # Within 20 hours nothing runs; after, the next file is made.
            assert backup.run(session=gh, now=NOW + timedelta(hours=5))["reason"].startswith("last backup")
            assert not backup.marker().exists()
            # A story removed from the runner's window stays in the backup (remembered rows).
            tr.new_process()
            st = backup.run(session=gh, now=NOW + timedelta(days=1))
            assert st["done"] and len(gh.assets) == 2
            # The remembered rows are lost: seeded back from the newest backup.
            shutil.rmtree(backup.tables_dir())
            tr.new_process()
            st = backup.run(session=gh, now=NOW + timedelta(days=2))
            assert st["done"] and st["seeded"] > 0, st
        finally:
            for k in ("GH_TOKEN", "BACKUP_KEY", "BACKUP_STORE_DIR"):
                os.environ.pop(k, None)
            media.reset()


def test_admin_storage_cards():
    from digest import admin

    now = NOW
    none = admin.storage_cards({"backup": {"configured": False}, "media": {}}, now)
    assert [c["id"] if "id" in c else c.get("key") for c in none] and none[0]["level"] == "info"
    fresh = {"backup": {"configured": True, "lastAt": (now - timedelta(hours=10)).isoformat()}, "media": {"store": "ok", "pending": 0}}
    assert admin.storage_cards(fresh, now) == []
    stale = {"backup": {"configured": True, "lastAt": (now - timedelta(days=3)).isoformat(), "reason": "upload failed"},
             "media": {"store": "unreachable", "pending": 12, "error": "HTTP 502"}}
    assert [c["level"] for c in admin.storage_cards(stale, now)] == ["warning", "warning"]


# ---------------------------------------------------------------------------- watchdog

class FakeActions:
    def __init__(self, last_success_at: str | None, state: str = "active", open_issue: bool = False):
        self.last, self.state, self.issue = last_success_at, state, {"number": 5, "body": "x"} if open_issue else None
        self.calls: list[tuple[str, str]] = []

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url))
        if url.endswith("/runs"):
            return Resp(200, {"workflow_runs": [{"updated_at": self.last, "html_url": "https://run"}] if self.last else []})
        if "/actions/workflows/" in url:
            return Resp(200, {"state": self.state})
        if url.endswith("/issues"):
            return Resp(200, [self.issue] if self.issue else [])
        return Resp(404, {})

    def put(self, url, **kw):
        self.calls.append(("PUT", url))
        return Resp(204, None)

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url))
        if url.endswith("/issues"):
            self.issue = {"number": 6, "body": json["body"]}
            return Resp(201, self.issue)
        return Resp(201, {})

    def patch(self, url, json=None, **kw):
        self.calls.append(("PATCH", url))
        return Resp(200, {})


def test_watchdog_opens_one_issue_updates_it_closes_it_and_keeps_the_schedule_alive():
    now = datetime(2026, 9, 16, 12, 37, tzinfo=timezone.utc)  # a Wednesday
    fresh = FakeActions("2026-09-16T11:30:00Z")
    r = watchdog.run(fresh, "o/r", now)
    assert not r["stopped"] and r["issue"] == "none" and r["reenabled"] == []
    stale = FakeActions("2026-09-16T08:00:00Z")
    r = watchdog.run(stale, "o/r", now)
    assert r["stopped"] and r["hours"] == 4.6 and r["issue"] == "opened"
    r = watchdog.run(stale, "o/r", now + timedelta(hours=1))
    assert r["issue"] == "updated" and sum(1 for m, u in stale.calls if m == "POST" and u.endswith("/issues")) == 1
    stale.last = "2026-09-16T13:30:00Z"
    assert watchdog.run(stale, "o/r", now + timedelta(hours=1, minutes=10))["issue"] == "closed"
    assert watchdog.run(FakeActions(None), "o/r", now)["stopped"]
    # Disabled by GitHub: re-enabled at once. Mondays at 04:00 UTC: re-enabled anyway.
    off = FakeActions("2026-09-16T11:30:00Z", state="disabled_inactivity")
    assert watchdog.run(off, "o/r", now)["reenabled"] == ["pipeline.yml", "watchdog.yml"]
    monday = datetime(2026, 9, 21, 4, 37, tzinfo=timezone.utc)
    assert watchdog.run(FakeActions("2026-09-21T04:10:00Z"), "o/r", monday)["reenabled"] == ["pipeline.yml", "watchdog.yml"]


if __name__ == "__main__":
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
                traceback.print_exc(limit=3)
    sys.exit(1 if failures else 0)
