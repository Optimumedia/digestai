"""Stories that left the export window keep a page.

The site builds full pages for stories updated in the last EXPORT_DAYS days. A story older than
that used to drop out of the build and its address returned 404, losing the page search engines
had indexed. Now, each run, the export notes the stories that have just aged out of the window
(bounded: a handful a run) and appends a compact record of each to archive.json, from which the
site builds a small archive page at the same address: headline, summary, key points and sources,
no full text, no share image.

The file lives with the runner's other copies in pipeline/data/cache (kept between runs by the
Actions cache). The media step also publishes a copy once a day as a release asset (tag
"archive"); a missing file is taken from there, and only when that fails too is it rebuilt from
the database (slug, headline, summary and key points of every published story older than the
window, plus the title and link of their articles: a large read once the archive is big).
Stories unpublished through moderation.yaml are removed from it.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from . import cache, config, db, media

log = logging.getLogger("digest.archive")

FORMAT = 1
SUMMARY_CHARS = 700
KEY_POINTS = 4
SOURCES = 6
RELEASE_TAG = "archive"
ASSET = "archive.json.gz"
MAX_GAP_DAYS = 9            # the runner's copy covers ten days past the window


def path() -> Path:
    return config.CACHE_DIR / "archive.json.gz"


def _load() -> dict:
    try:
        with gzip.open(path(), "rt", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") == FORMAT and isinstance(data.get("stories"), dict):
            return data
    except (OSError, ValueError, EOFError):
        pass
    return {"format": FORMAT, "windowStart": None, "stories": {}}


def _save(data: dict) -> None:
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_name(path().name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), default=str)
    tmp.replace(path())


def _download() -> dict | None:
    """The copy the media step published, when running in GitHub Actions (or a test hook is set)."""
    if not (media.FETCH or os.environ.get("GITHUB_ACTIONS") == "true"):
        return None
    raw = media.content_at(media.asset_url(RELEASE_TAG, ASSET))
    try:
        data = json.loads(gzip.decompress(raw).decode("utf-8")) if raw else None
    except (OSError, ValueError, EOFError):
        return None
    if data and data.get("format") == FORMAT and isinstance(data.get("stories"), dict) and data.get("windowStart"):
        log.info("archive taken from the published copy: %d stories", len(data["stories"]))
        return data
    return None


def plain(md: str | None, limit: int) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md or "")
    text = re.sub(r"[#*_`>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _iso(dt) -> str | None:
    dt = db.as_utc(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def entry(story, articles: list, sources: dict, left_at: str) -> dict:
    """The compact record of one story. `story` and `articles` carry the columns the export reads."""
    points = story.key_points if isinstance(story.key_points, list) else []
    srcs = []
    seen = set()
    for a in articles:
        if not a.url or a.url in seen:
            continue
        seen.add(a.url)
        src = sources.get(a.source_id, {})
        srcs.append({"title": plain(a.title or a.headline or a.domain, 160), "url": a.url,
                     "source": (a.domain if src.get("discovered") or src.get("type") == "community" else src.get("name")) or a.domain})
        if len(srcs) >= SOURCES:
            break
    return {
        "slug": story.slug,
        "headline": story.headline,
        "summary": plain(story.summary_md, SUMMARY_CHARS),
        "keyPoints": [plain(p, 240) for p in points[:KEY_POINTS] if p],
        "category": story.category,
        "categoryName": config.CATEGORIES.get(story.category or "", "AI"),
        "firstPublishedAt": _iso(story.first_published_at),
        "updatedAt": _iso(story.updated_at),
        "archivedAt": left_at,
        "sources": srcs,
    }


def _rebuild(conn, since: datetime, sources: dict, now: datetime) -> dict[str, dict]:
    """Every published story older than the window, read narrowly from the database."""
    s, a = db.stories.c, db.articles.c
    rows = conn.execute(select(s.id, s.slug, s.headline, s.summary_md, s.key_points, s.category, s.first_published_at, s.updated_at)
                        .where(s.status == "published", s.updated_at < since).order_by(s.id)).all()
    if not rows:
        return {}
    by_story: dict[int, list] = {}
    ids = [r.id for r in rows]
    for k in range(0, len(ids), 500):
        arts = conn.execute(select(a.id, a.story_id, a.source_id, a.url, a.title, a.headline, a.domain, a.published_at)
                            .where(a.status == "published", a.story_id.in_(ids[k:k + 500]))).all()
        for r in arts:
            by_story.setdefault(r.story_id, []).append(r)
    out = {}
    left = _iso(now)
    for r in rows:
        arts = sorted(by_story.get(r.id, []), key=lambda x: (x.published_at is None, db.as_utc(x.published_at) if x.published_at else now), reverse=True)
        if not arts:
            continue
        out[r.slug] = entry(r, arts, sources, left)
    log.info("archive rebuilt from the database: %d stories", len(out))
    return out


def update(conn, now: datetime, since: datetime, sources: dict, unpublished: list[str] | None = None) -> dict:
    """Called by the export with this run's window start. Stories whose updated_at moved out of the
    window since the previous run are appended; their rows are still in the runner's copy of the
    mirror (kept ten days past the window) and their texts were read last run, so this costs
    nothing to read in the normal case."""
    data = _load()
    stats = {"added": 0, "removed": 0, "total": 0, "rebuilt": False}
    if not data.get("windowStart"):
        downloaded = _download()
        if downloaded:
            data, stats["downloaded"] = downloaded, True
    prev = datetime.fromisoformat(data["windowStart"]) if data.get("windowStart") else None
    stale = prev is None or prev > since or since - prev > timedelta(days=MAX_GAP_DAYS)
    if stale:
        data["stories"] = _rebuild(conn, since, sources, now)
        stats["rebuilt"] = True
        stats["added"] = len(data["stories"])
    else:
        leaving = [s for s in cache.stories(conn).values()
                   if s.status == "published" and prev <= (db.as_utc(s.updated_at) or now) < since]
        if leaving:
            texts = cache.story_text(conn, leaving)
            ids = {s.id for s in leaving}
            arts = [a for a in cache.articles(conn).values() if a.status == "published" and a.story_id in ids]
            a_text = cache.article_text(conn, arts)
            by_story: dict[int, list] = {}
            for a in sorted(arts, key=lambda a: (a.published_at is None, db.as_utc(a.published_at) if a.published_at else now), reverse=True):
                if a.id in a_text:
                    by_story.setdefault(a.story_id, []).append(cache.merged(a, a_text[a.id]))
            left = _iso(now)
            for s in leaving:
                if s.id in texts and by_story.get(s.id):
                    data["stories"][texts[s.id].slug] = entry(cache.merged(s, texts[s.id]), by_story[s.id], sources, left)
                    stats["added"] += 1
    for slug in unpublished or []:
        if data["stories"].pop(slug, None) is not None:
            stats["removed"] += 1
    data["windowStart"] = since.isoformat()
    _save(data)
    stats["total"] = len(data["stories"])
    return stats


def export(out_dir: Path, live_slugs: set[str]) -> int:
    """archive.json for the site: archived stories that have no live page (a story can come back
    into the window when a new article lands in it)."""
    data = _load()
    rows = sorted((e for slug, e in data["stories"].items() if slug not in live_slugs),
                  key=lambda e: e.get("firstPublishedAt") or "", reverse=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "archive.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(rows)
