"""Step: render a 1200x630 share card per story so links look like us on X, LinkedIn and Slack,
and a ~600 px WebP thumbnail of each story's picture so the front page does not hot-link
publishers' full-size images (slow, and it sent readers' addresses to the publisher).

Share images and thumbnails go to the media store (media.py): rendered or fetched once, uploaded
once, never kept in the site tree or the per-run cache. Thread and topic cards are few and
change with their counts, so they are simply rendered into site/public/og on every run.
"""
from __future__ import annotations

import io
import json
import logging
import textwrap
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from . import config, media

log = logging.getLogger("digest.images")

FONTS = config.PIPELINE_DIR / "assets" / "fonts"
OUT = config.ROOT / "site" / "public" / "og"
W, H = 1200, 630
MAX_PER_RUN = 150            # share images rendered per run (a backlog clears in a few runs)
MAX_CARDS_PER_RUN = 300      # thread and topic cards, re-rendered every run
MAX_THUMBS_PER_RUN = 40      # publisher pictures fetched per run
THUMB_TIME_BUDGET_SECONDS = 60
THUMB_WIDTH = 600
THUMB_MIN_WIDTH = 300        # smaller pictures are logos and icons; the page keeps the original
THUMB_MAX_BYTES = 8_000_000  # a download larger than this is not a picture for a news list
THUMB_TIMEOUT = (5, 10)

CATEGORY_COLORS = {
    "models": "#4f6cf0", "agents": "#12a37a", "research": "#8b5cf6", "business": "#e0961b",
    "policy": "#e5484d", "hardware": "#0ea5c4", "enterprise": "#8892a0", "robotics": "#e04c8e",
    "society": "#7cb518",
}


def _font(name: str, size: int, weight: int, opsz: int | None = None):
    f = ImageFont.truetype(str(FONTS / name), size)
    try:
        axes = [weight] + ([opsz] if opsz is not None else [])
        f.set_variation_by_axes(axes)
    except Exception:  # noqa: BLE001 - static fallback is fine
        pass
    return f


def render(story: dict, path: Path) -> None:
    img = Image.new("RGB", (W, H), "#0e1116")
    d = ImageDraw.Draw(img)
    color = CATEGORY_COLORS.get(story.get("category") or "", "#4f6cf0")
    d.rectangle([0, 0, 16, H], fill=color)

    mono = _font("JetBrainsMono.ttf", 24, 500)
    brand_serif = _font("Newsreader.ttf", 44, 600, 72)
    body = _font("SourceSans3.ttf", 28, 400)

    # Category and brand line.
    d.text((80, 62), (story.get("categoryName") or "AI").upper(), font=mono, fill=color)
    d.text((W - 80 - d.textlength("Digest", font=brand_serif) - d.textlength(" AI", font=brand_serif), 48), "Digest", font=brand_serif, fill="#e6eaf0")
    d.text((W - 80 - d.textlength(" AI", font=brand_serif), 48), " AI", font=brand_serif, fill="#8397ff")

    # Headline: shrink until it fits in three lines.
    headline = story["headline"]
    for size in (76, 68, 60, 54, 48):
        font = _font("Newsreader.ttf", size, 500, 72)
        chars = int((W - 160) / (size * 0.42))
        lines = textwrap.wrap(headline, width=chars)
        if len(lines) <= 3:
            break
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][: max(0, len(lines[-1]) - 1)] + "…"
    y = 140
    for line in lines:
        d.text((80, y), line, font=font, fill="#f2f4f7")
        y += int(size * 1.15)

    # Digest line.
    summary = (story.get("keyPoints") or [None])[0] or ""
    if summary:
        for line in textwrap.wrap(summary, width=78)[:2]:
            d.text((80, y + 16), line, font=body, fill="#aab3bf")
            y += 38

    # Footer: coverage.
    cov = story.get("coverage") or {}
    parts = [f"{story.get('articleCount', 1)} source" + ("s" if story.get("articleCount", 1) != 1 else "")]
    if cov.get("primary"):
        parts.append("primary source inside")
    if story.get("discussions"):
        pts = story["discussions"][0].get("points")
        parts.append(f"HN {pts} points" if pts else "discussed on HN")
    date = (story.get("firstPublishedAt") or "")[:10]
    if date:
        parts.append(date)
    d.text((80, H - 70), "  ·  ".join(parts), font=mono, fill="#78828f")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)


def render_card(kind: str, title: str, subtitle: str, footer: str, color: str, path: Path) -> None:
    """Card for a thread or topic page: eyebrow, big title, a line of context."""
    img = Image.new("RGB", (W, H), "#0e1116")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 16, H], fill=color)
    mono = _font("JetBrainsMono.ttf", 24, 500)
    brand_serif = _font("Newsreader.ttf", 44, 600, 72)
    body = _font("SourceSans3.ttf", 30, 400)
    d.text((80, 62), kind.upper(), font=mono, fill=color)
    d.text((W - 80 - d.textlength("Digest AI", font=brand_serif), 48), "Digest", font=brand_serif, fill="#e6eaf0")
    d.text((W - 80 - d.textlength(" AI", font=brand_serif), 48), " AI", font=brand_serif, fill="#8397ff")
    for size in (72, 64, 56, 48):
        font = _font("Newsreader.ttf", size, 500, 72)
        lines = textwrap.wrap(title, width=int((W - 160) / (size * 0.42)))
        if len(lines) <= 3:
            break
    y = 150
    for line in lines[:3]:
        d.text((80, y), line, font=font, fill="#f2f4f7")
        y += int(size * 1.15)
    for line in textwrap.wrap(subtitle, width=72)[:2]:
        d.text((80, y + 14), line, font=body, fill="#aab3bf")
        y += 40
    d.text((80, H - 70), footer, font=mono, fill="#78828f")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)


# ---- thumbnails -----------------------------------------------------------------------

def thumbnail(data: bytes, width: int = THUMB_WIDTH) -> bytes | None:
    """A WebP no wider than `width` (and no taller than 1.5x that), or None for pictures too small to be worth it."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        if img.width < THUMB_MIN_WIDTH or img.height < 120:
            return None
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGBA") if "A" in img.getbands() or img.mode == "P" else img.convert("RGB")
            if img.mode == "RGBA":
                flat = Image.new("RGB", img.size, "#ffffff")
                flat.paste(img, mask=img.getchannel("A"))
                img = flat
        scale = min(1.0, width / img.width, width * 1.5 / img.height)
        if scale < 1.0:
            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
        for quality in (78, 62):
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=quality, method=4)
            if buf.tell() <= 120_000:
                break
        return buf.getvalue()


def fetch_picture(url: str, fetch=None) -> bytes:
    """The picture behind a story's image link. No Referer is sent (the site shows the thumbnail
    with referrerpolicy no-referrer for the same reason). Raises on anything but an image."""
    if fetch is not None:
        return fetch(url)
    headers = {"User-Agent": config.USER_AGENT, "Accept": "image/*,*/*;q=0.5"}
    with requests.get(url, headers=headers, timeout=THUMB_TIMEOUT, stream=True, allow_redirects=True) as r:
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith("image/"):
            raise ValueError(f"not an image ({ctype})")
        if ctype in ("image/svg+xml",):
            raise ValueError("svg")
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > THUMB_MAX_BYTES:
                raise ValueError("too large")
        return data


def make_thumbnails(stories: list[dict], fetch=None, budget_seconds: float = THUMB_TIME_BUDGET_SECONDS,
                    limit: int = MAX_THUMBS_PER_RUN) -> dict:
    """Newest stories first; a picture that fails is tried again a day later, three times at most."""
    stats = {"made": 0, "failed": 0, "skipped": 0}
    t0 = time.time()
    order = sorted((s for s in stories if s.get("imageUrl")), key=lambda s: s.get("firstPublishedAt") or "", reverse=True)
    for s in order:
        name = f"thumb-{s['slug']}.webp"
        if media.has(name) or media.skipped_recently(name):
            stats["skipped"] += 1
            continue
        if stats["made"] + stats["failed"] >= limit or time.time() - t0 > budget_seconds:
            break
        try:
            data = fetch_picture(s["imageUrl"], fetch)
            out = thumbnail(data)
            if out is None:
                media.mark_skipped(name, "too small")
                stats["failed"] += 1
                continue
            media.queue(name, out)
            stats["made"] += 1
        except Exception as exc:  # noqa: BLE001 - a picture that fails is skipped, never a crash
            media.mark_skipped(name, f"{type(exc).__name__}: {str(exc)[:60]}")
            stats["failed"] += 1
    return stats


# ---- step -----------------------------------------------------------------------------

def run(fetch=None) -> dict:
    stats = {"rendered": 0, "skipped": 0, "cards": 0}
    data = config.SITE_DATA_DIR / "stories.json"
    if not data.exists():
        return stats
    stories = json.loads(data.read_text(encoding="utf-8"))
    for story in stories:
        name = f"og-{story['slug']}.png"
        if media.has(name):
            stats["skipped"] += 1
            continue
        if stats["rendered"] >= MAX_PER_RUN:
            break
        try:
            render(story, media.pending_path(name))
            stats["rendered"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("share image failed for %s: %s", story["slug"], exc)

    stats["thumbs"] = make_thumbnails(stories, fetch)
    media.save_manifest()

    # Thread and topic cards: few, cheap, and their counts change, so they are drawn fresh each run.
    OUT.mkdir(parents=True, exist_ok=True)
    threads_file = config.SITE_DATA_DIR / "threads.json"
    if threads_file.exists():
        for t in json.loads(threads_file.read_text(encoding="utf-8")):
            if stats["cards"] >= MAX_CARDS_PER_RUN:
                break
            try:
                render_card("Developing story", t["title"], t.get("summary") or "", f"{t['storyCount']} episodes  ·  {(t.get('firstAt') or '')[:10]} to {(t.get('updatedAt') or '')[:10]}",
                            CATEGORY_COLORS.get(t.get("category") or "", "#4f6cf0"), OUT / f"thread-{t['slug']}.png")
                stats["cards"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("thread card failed for %s: %s", t["slug"], exc)
    entities_file = config.SITE_DATA_DIR / "entities.json"
    if entities_file.exists():
        from .textutil import slugify

        for e in json.loads(entities_file.read_text(encoding="utf-8")):
            if len(e.get("storyIds") or []) < 3:
                continue
            if stats["cards"] >= MAX_CARDS_PER_RUN:
                break
            slug = slugify(e["name"])
            n = len(e["storyIds"])
            try:
                kind = {"companies": "Company", "models": "Model", "people": "Person"}.get(e.get("kind"), "Topic")
                render_card(kind, e["name"], f"Every story about {e['name']} on Digest AI, with sources and discussion.", f"{n} stories  ·  digestai.news/topic/{slug}", "#4f6cf0", OUT / f"topic-{slug}.png")
                stats["cards"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("topic card failed for %s: %s", e["name"], exc)
    return stats
