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

When a story's primary source is the company's own announcement and its picture is at least 1200 px
wide, the story page uses that picture as its share image instead of the card (primary_images below).
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
from urllib.parse import urlsplit

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


# ---- the company's own share image ------------------------------------------------------
# When a story's primary source is the company's own announcement (openai.com, blog.google, ...), the
# picture that page declares is a press image the company publishes to be shared, so the story page
# may use it as its og:image instead of our card. Never a news outlet's photo (those are usually
# licensed from agencies), and only when it is big enough for a large link preview. The picture stays
# on the company's server: the page links to it and this step only reads its header for the size and
# checks it still answers (a failure puts our card back on the next build).

PRIMARY_FILE = "primary-images.json"   # site/src/data: slug -> {url, width, height, ideal}
PRIMARY_MIN_WIDTH = 1200               # og and Discover both ask for 1200 px or more
PRIMARY_IDEAL_RATIO = (1.5, 2.1)       # close to 1.91:1; any other shape is cropped by the networks
PRIMARY_RECHECK_HOURS = 24             # a picture that answered is checked again a day later
PRIMARY_RETRY_HOURS = 24               # and one that failed is tried again a day later
MAX_PRIMARY_CHECKS_PER_RUN = 30
PRIMARY_TIME_BUDGET_SECONDS = 30
PRIMARY_HEAD_BYTES = 512_000           # the size is in the first bytes; nothing more is downloaded
PRIMARY_TIMEOUT = (5, 10)

# The makers' own sites. The primary domains that are not a company (arXiv, GitHub, governments) are
# left out, and a few company sites the feeds do not tag as primary yet are added, so they count
# the day they are. An article qualifies only when the export also marked it primary.
NOT_COMPANY = frozenset({"arxiv.org", "github.com", "qwenlm.github.io", "europa.eu", "whitehouse.gov",
                         "gov.uk", "nist.gov", "ftc.gov", "sec.gov"})
COMPANY_DOMAINS = frozenset((config.PRIMARY_DOMAINS - NOT_COMPANY) | {
    "google", "amazon.com", "aboutamazon.com", "canva.com", "hubspot.com", "shopify.com", "adobe.com",
    "salesforce.com", "ibm.com", "intel.com", "amd.com", "samsung.com", "qualcomm.com", "oracle.com",
})
# Hosts on a company domain whose pages are not the company speaking: model cards, Spaces and
# datasets on Hugging Face are uploaded by anyone, so only its blog counts.
COMPANY_PATHS = {"huggingface.co": "/blog/"}


def _on_domain(host: str, domains) -> str | None:
    host = (host or "").lower().removeprefix("www.")
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


def company_announcement(article: dict) -> bool:
    """True when the article is the company's own announcement: marked primary by the export and on
    one of the makers' own domains (not a preprint, a repository, a government or a news outlet)."""
    if article.get("sourceType") != "primary":
        return False
    host = (article.get("domain") or urlsplit(article.get("url") or "").netloc or "").lower()
    d = _on_domain(host, COMPANY_DOMAINS)
    if not d:
        return False
    need = COMPANY_PATHS.get(d)
    return not need or need in urlsplit(article.get("url") or "").path


def primary_candidate(story: dict) -> dict | None:
    """The company announcement among the story's articles that has an https picture, lead first."""
    arts = sorted(story.get("articles") or [], key=lambda a: not a.get("isLead"))
    for a in arts:
        url = (a.get("imageUrl") or "").strip()
        if url.lower().startswith("https://") and company_announcement(a):
            return a
    return None


def probe_image(url: str, fetch=None) -> dict:
    """Does the picture answer (https, 200, an image type), and how big is it? Reads only the header.
    {"ok": bool, "width": int, "height": int, "type": str, "error": str}. `fetch(url)` in tests returns
    (status, content_type, bytes, final_url)."""
    if not url.lower().startswith("https://"):
        return {"ok": False, "error": "not https"}
    try:
        if fetch is not None:
            status, ctype, head, final = fetch(url)
        else:
            headers = {"User-Agent": config.USER_AGENT, "Accept": "image/*,*/*;q=0.5"}
            with requests.get(url, headers=headers, timeout=PRIMARY_TIMEOUT, stream=True, allow_redirects=True) as r:
                status, ctype, final = r.status_code, r.headers.get("Content-Type") or "", r.url
                head, size = b"", None
                if status == 200:
                    for chunk in r.iter_content(16384):
                        head += chunk
                        size = _image_size(head)
                        if size or len(head) >= PRIMARY_HEAD_BYTES:
                            break
        ctype = (ctype or "").split(";")[0].strip().lower()
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        if not (final or url).lower().startswith("https://"):
            return {"ok": False, "error": "redirected off https"}
        if not ctype.startswith("image/") or ctype == "image/svg+xml":
            return {"ok": False, "error": f"not an image ({ctype or 'no type'})"}
        size = _image_size(head)
        if not size:
            return {"ok": False, "error": "size unreadable"}
        return {"ok": True, "width": size[0], "height": size[1], "type": ctype}
    except Exception as exc:  # noqa: BLE001 - a picture that fails means our card, never a crash
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:60]}"}


def _image_size(head: bytes) -> tuple[int, int] | None:
    """Width and height from the first bytes of a picture (PIL reads only the header), or None."""
    try:
        with Image.open(io.BytesIO(head)) as img:
            return img.size
    except Exception:  # noqa: BLE001 - not enough bytes yet, or not a format PIL knows
        return None


def primary_choice(url: str, entry: dict | None) -> dict | None:
    """What the site gets for a probed picture: {url, width, height, ideal}, or None for our card."""
    if not entry or not entry.get("ok"):
        return None
    w, h = int(entry.get("width") or 0), int(entry.get("height") or 0)
    if w < PRIMARY_MIN_WIDTH or h <= 0:
        return None
    lo, hi = PRIMARY_IDEAL_RATIO
    return {"url": url, "width": w, "height": h, "ideal": lo <= w / h <= hi}


def _hours_since(stamp: str | None, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return float("inf")


def primary_images(stories: list[dict], now: datetime, fetch=None, cache_path: Path | None = None,
                   out_path: Path | None = None, limit: int = MAX_PRIMARY_CHECKS_PER_RUN,
                   budget_seconds: float = PRIMARY_TIME_BUDGET_SECONDS) -> dict:
    """Write site/src/data/primary-images.json for the stories whose company announcement has a picture
    that answers and is at least 1200 px wide. Results are kept by URL in the runner cache, so a picture
    is measured once and re-checked once a day, newest stories first, within the run's budget. A story
    whose picture has not been checked yet, or failed its last check, keeps our card."""
    cache_path = cache_path or config.CACHE_DIR / PRIMARY_FILE
    out_path = out_path or config.SITE_DATA_DIR / PRIMARY_FILE
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    stats = {"candidates": 0, "checked": 0, "failed": 0, "used": 0, "ideal": 0}
    t0 = time.time()
    out: dict[str, dict] = {}
    live_urls = set()
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or "", reverse=True):
        art = primary_candidate(s)
        if not art:
            continue
        url = art["imageUrl"].strip()
        live_urls.add(url)
        stats["candidates"] += 1
        entry = cache.get(url)
        wait = PRIMARY_RECHECK_HOURS if entry and entry.get("ok") else PRIMARY_RETRY_HOURS
        due = entry is None or _hours_since(entry.get("checked"), now) >= wait
        if due and stats["checked"] < limit and time.time() - t0 <= budget_seconds:
            probed = probe_image(url, fetch)
            entry = {**(entry or {}), **probed, "checked": now.isoformat()[:19] + "Z"}
            if probed.get("ok"):
                entry.pop("error", None)
            cache[url] = entry
            stats["checked"] += 1
            stats["failed"] += int(not probed.get("ok"))
        choice = primary_choice(url, entry)
        if choice:
            out[s["slug"]] = choice
            stats["used"] += 1
            stats["ideal"] += int(choice["ideal"])
    # The cache keeps only the pictures of stories still in the export.
    cache = {u: e for u, e in cache.items() if u in live_urls}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
    except OSError as exc:
        log.warning("primary image cache not saved: %s", exc)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=0, sort_keys=True), encoding="utf-8")
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
    try:
        stats["primary"] = primary_images(stories, now)
    except Exception as exc:  # noqa: BLE001 - without the file every story keeps our card
        log.warning("primary share images failed: %s", exc)

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
