"""Step: render a 1200x630 share card per story so links look like us on X, LinkedIn and Slack,
and a ~600 px WebP thumbnail of each story's picture so the front page does not hot-link
publishers' full-size images (slow, and it sent readers' addresses to the publisher).

Cards are 64-colour PNGs (~25 KB instead of ~67 KB; flat colours and text lose nothing).

Every share image and thumbnail goes to the media store (media.py) once. The store serves files as
downloads without an image type, which Facebook and LinkedIn refuse as a link preview, so the
cards of stories from the last OG_PAGES_DAYS days (when links get shared) are also kept in
site/public/og and served by Pages; older stories fall back to the default card. Thread and topic
cards live there too, re-rendered when their counts change. site/public/og is kept between runs
by a cache the workflow saves once a day; anything missing is rendered again.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from . import config, media
from .textutil import slugify

log = logging.getLogger("digest.images")

FONTS = config.PIPELINE_DIR / "assets" / "fonts"
OUT = config.ROOT / "site" / "public" / "og"
W, H = 1200, 630
MAX_PER_RUN = 150            # share images rendered per run (a backlog clears in a few runs)
OG_PAGES_DAYS = 7            # share images of stories this recent are also served from Pages
# The same card at the shapes Google asks for in a NewsArticle's image (Discover crops to them). Pages
# only, never the media store, and only while a story is fresh enough for Discover: ~110 KB a story.
VARIANTS = {"16x9": (1200, 675), "4x3": (1200, 900), "1x1": (1200, 1200)}
VARIANT_DAYS = 3
MAX_VARIANTS_PER_RUN = 150   # variant files per run, a budget of their own so share cards never wait
CARD_COLORS = 64
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
    "society": "#7cb518", "marketing": "#f97316",
}


def _save_card(img: Image.Image, path: Path) -> None:
    img.quantize(CARD_COLORS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)


@lru_cache(maxsize=None)
def _font(name: str, size: int, weight: int, opsz: int | None = None):
    """Loaded once per (face, size, axes): a run renders hundreds of cards with the same few fonts."""
    f = ImageFont.truetype(str(FONTS / name), size)
    try:
        axes = [weight] + ([opsz] if opsz is not None else [])
        f.set_variation_by_axes(axes)
    except Exception:  # noqa: BLE001 - static fallback is fine
        pass
    return f


# Characters the card fonts have no glyph for (they drew as empty boxes), and what to draw instead.
_GLYPHS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", " ": " ", " ": " ", " ": " "})


def _wrap(d: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    """Lines no wider than `width` pixels, measured with the font (a guess from the character count
    let long words run off the card's right edge). A single word wider than the card gets its own line."""
    lines: list[str] = []
    for word in text.translate(_GLYPHS).split():
        if lines and d.textlength(f"{lines[-1]} {word}", font=font) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def render(story: dict, path: Path, size: tuple[int, int] = (W, H)) -> None:
    """The story's card. The default size is the 1200x630 share card; VARIANTS are the same card
    at 16:9, 4:3 and 1:1 for Google (Discover and the NewsArticle image), with the headline allowed
    more lines and the block placed lower as the card gets taller."""
    W, H = size  # noqa: N806 - same names as the module's card size
    img = Image.new("RGB", (W, H), "#0e1116")
    d = ImageDraw.Draw(img)
    color = CATEGORY_COLORS.get(story.get("category") or "", "#4f6cf0")
    d.rectangle([0, 0, 16, H], fill=color)
    extra = H - 630                        # room a taller card has over the share card
    max_lines = 3 + extra // 190           # 630: 3, 675: 3, 900: 4, 1200: 6
    sizes = (76, 68, 60, 54, 48) if extra < 200 else (84, 76, 68, 60, 54, 48)

    mono = _font("JetBrainsMono.ttf", 24, 500)
    brand_serif = _font("Newsreader.ttf", 44, 600, 72)
    body = _font("SourceSans3.ttf", 28, 400)

    # Category and brand line.
    d.text((80, 62), (story.get("categoryName") or "AI").upper(), font=mono, fill=color)
    d.text((W - 80 - d.textlength("Digest", font=brand_serif) - d.textlength(" AI", font=brand_serif), 48), "Digest", font=brand_serif, fill="#e6eaf0")
    d.text((W - 80 - d.textlength(" AI", font=brand_serif), 48), " AI", font=brand_serif, fill="#8397ff")

    # Headline: shrink until it fits in the card's lines.
    headline = story["headline"]
    for size in sizes:
        font = _font("Newsreader.ttf", size, 500, 72)
        lines = _wrap(d, headline, font, W - 160)
        if len(lines) <= max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and d.textlength(last + "…", font=font) > W - 160:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    y = 140 + int(extra * 0.3)
    for line in lines:
        d.text((80, y), line, font=font, fill="#f2f4f7")
        y += int(size * 1.15)

    # Digest line.
    summary = (story.get("keyPoints") or [None])[0] or ""
    if summary:
        for line in _wrap(d, summary, body, W - 160)[:2 + extra // 300]:
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
    _save_card(img, path)


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
        lines = _wrap(d, title, font, W - 160)
        if len(lines) <= 3:
            break
    y = 150
    for line in lines[:3]:
        d.text((80, y), line, font=font, fill="#f2f4f7")
        y += int(size * 1.15)
    for line in _wrap(d, subtitle, body, W - 160)[:2]:
        d.text((80, y + 14), line, font=body, fill="#aab3bf")
        y += 40
    d.text((80, H - 70), footer, font=mono, fill="#78828f")
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_card(img, path)


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

def _recent(story: dict, now: datetime, days: float = OG_PAGES_DAYS) -> bool:
    try:
        first = datetime.fromisoformat((story.get("firstPublishedAt") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return first >= now - timedelta(days=days)


def render_variants(story: dict, recent: set, budget: int) -> int:
    """The story's 16:9, 4:3 and 1:1 cards in site/public/og, those not drawn yet, within budget.
    Every variant name is added to `recent` so the prune below keeps it. Returns how many were drawn."""
    drawn = 0
    for key, size in VARIANTS.items():
        page = OUT / f"{story['slug']}-{key}.png"
        recent.add(page.name)
        if page.exists() or drawn >= budget:
            continue
        render(story, page, size)
        drawn += 1
    return drawn


def _card_once(path: Path, stamp: Path, draw) -> bool:
    """Draw a thread or topic card unless its stamp (the count it was drawn for) exists."""
    if stamp.exists() and path.exists():
        return False
    draw(path)
    for old in path.parent.glob(f"{path.stem}.*.stamp"):
        old.unlink()
    stamp.touch()
    return True


def run(fetch=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"rendered": 0, "skipped": 0, "pages": 0, "pruned": 0, "cards": 0, "variants": 0}
    data = config.SITE_DATA_DIR / "stories.json"
    if not data.exists():
        return stats
    stories = json.loads(data.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    budget = MAX_PER_RUN
    variant_budget = MAX_VARIANTS_PER_RUN
    recent = set()
    # Newest first, so a backlog never delays the cards of today's stories.
    for story in sorted(stories, key=lambda s: s.get("firstPublishedAt") or "", reverse=True):
        slug = story["slug"]
        name = f"og-{slug}.png"
        page = OUT / f"{slug}.png"
        in_window = _recent(story, now)
        if in_window:
            recent.add(page.name)
        if _recent(story, now, VARIANT_DAYS):
            try:
                n = render_variants(story, recent, variant_budget)
                variant_budget -= n
                stats["variants"] += n
            except Exception as exc:  # noqa: BLE001 - a variant is a nice-to-have, never a failed step
                log.warning("card variants failed for %s: %s", slug, exc)
        need_store, need_page = not media.has(name), in_window and not page.exists()
        if not (need_store or need_page):
            stats["skipped"] += 1
            continue
        if budget <= 0:
            continue
        try:
            pending = media.pending_path(name)
            if need_store:
                render(story, pending)
                stats["rendered"] += 1
                budget -= 1
            if need_page:
                if pending.exists():
                    shutil.copyfile(pending, page)
                else:
                    render(story, page)
                    budget -= 1
                stats["pages"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("share image failed for %s: %s", slug, exc)
    # Story cards older than the window leave Pages (the store keeps them).
    for f in OUT.glob("*.png"):
        if not f.name.startswith(("thread-", "topic-")) and f.name not in recent:
            f.unlink(missing_ok=True)
            stats["pruned"] += 1

    stats["thumbs"] = make_thumbnails(stories, fetch)
    media.save_manifest()

    threads_file = config.SITE_DATA_DIR / "threads.json"
    live = set()
    if threads_file.exists():
        for t in json.loads(threads_file.read_text(encoding="utf-8")):
            path = OUT / f"thread-{t['slug']}.png"
            live.add(path.name)
            if stats["cards"] >= MAX_CARDS_PER_RUN:
                continue
            try:
                drawn = _card_once(path, OUT / f"thread-{t['slug']}.{t['storyCount']}.stamp", lambda p, t=t: render_card(
                    "Developing story", t["title"], t.get("summary") or "",
                    f"{t['storyCount']} episodes  ·  {(t.get('firstAt') or '')[:10]} to {(t.get('updatedAt') or '')[:10]}",
                    CATEGORY_COLORS.get(t.get("category") or "", "#4f6cf0"), p))
                stats["cards"] += int(drawn)
            except Exception as exc:  # noqa: BLE001
                log.warning("thread card failed for %s: %s", t["slug"], exc)
    entities_file = config.SITE_DATA_DIR / "entities.json"
    if entities_file.exists():
        for e in json.loads(entities_file.read_text(encoding="utf-8")):
            if len(e.get("storyIds") or []) < 3:
                continue
            slug = slugify(e["name"])
            n = len(e["storyIds"])
            path = OUT / f"topic-{slug}.png"
            live.add(path.name)
            if stats["cards"] >= MAX_CARDS_PER_RUN:
                continue
            try:
                kind = {"companies": "Company", "models": "Model", "people": "Person"}.get(e.get("kind"), "Topic")
                drawn = _card_once(path, OUT / f"topic-{slug}.{n}.stamp", lambda p, e=e, kind=kind, slug=slug, n=n: render_card(
                    kind, e["name"], f"Every story about {e['name']} on Digest AI, with sources and discussion.",
                    f"{n} stories  ·  digestai.news/topic/{slug}", "#4f6cf0", p))
                stats["cards"] += int(drawn)
            except Exception as exc:  # noqa: BLE001
                log.warning("topic card failed for %s: %s", e["name"], exc)
    # Cards of threads and topics that no longer have a page.
    for f in list(OUT.glob("thread-*")) + list(OUT.glob("topic-*")):
        base = f.name.split(".")[0] + ".png"
        if base not in live:
            f.unlink(missing_ok=True)
    return stats
