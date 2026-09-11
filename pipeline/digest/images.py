"""Step: render a 1200x630 share card per story so links look like us on X, LinkedIn and Slack."""
from __future__ import annotations

import json
import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config

log = logging.getLogger("digest.images")

FONTS = config.PIPELINE_DIR / "assets" / "fonts"
OUT = config.ROOT / "site" / "public" / "og"
W, H = 1200, 630
MAX_PER_RUN = 400

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
    img.save(path, optimize=True)


def run() -> dict:
    stats = {"rendered": 0, "skipped": 0, "cards": 0}
    data = config.SITE_DATA_DIR / "stories.json"
    if not data.exists():
        return stats
    stories = json.loads(data.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    for story in stories:
        path = OUT / f"{story['slug']}.png"
        if path.exists():
            stats["skipped"] += 1
            continue
        if stats["rendered"] >= MAX_PER_RUN:
            break
        try:
            render(story, path)
            stats["rendered"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("share image failed for %s: %s", story["slug"], exc)

    # Thread cards are re-rendered when the episode count changes; topic cards when stories are added.
    threads_file = config.SITE_DATA_DIR / "threads.json"
    if threads_file.exists():
        for t in json.loads(threads_file.read_text(encoding="utf-8")):
            path = OUT / f"thread-{t['slug']}.png"
            stamp = OUT / f"thread-{t['slug']}.{t['storyCount']}.stamp"
            if stamp.exists():
                continue
            try:
                render_card("Developing story", t["title"], t.get("summary") or "", f"{t['storyCount']} episodes  ·  {(t.get('firstAt') or '')[:10]} to {(t.get('updatedAt') or '')[:10]}",
                            CATEGORY_COLORS.get(t.get("category") or "", "#4f6cf0"), path)
                for old in OUT.glob(f"thread-{t['slug']}.*.stamp"):
                    old.unlink()
                stamp.touch()
                stats["cards"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("thread card failed for %s: %s", t["slug"], exc)
    entities_file = config.SITE_DATA_DIR / "entities.json"
    if entities_file.exists():
        from .textutil import slugify

        for e in json.loads(entities_file.read_text(encoding="utf-8")):
            if len(e.get("storyIds") or []) < 3:
                continue
            slug = slugify(e["name"])
            n = len(e["storyIds"])
            path = OUT / f"topic-{slug}.png"
            stamp = OUT / f"topic-{slug}.{n}.stamp"
            if stamp.exists():
                continue
            try:
                kind = {"companies": "Company", "models": "Model", "people": "Person"}.get(e.get("kind"), "Topic")
                render_card(kind, e["name"], f"Every story about {e['name']} on Digest AI, with sources and discussion.", f"{n} stories  ·  digestai.news/topic/{slug}", "#4f6cf0", path)
                for old in OUT.glob(f"topic-{slug}.*.stamp"):
                    old.unlink()
                stamp.touch()
                stats["cards"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("topic card failed for %s: %s", e["name"], exc)
    return stats
