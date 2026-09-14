"""Step: post to Digest AI's Bluesky account.

Two kinds of post, both built from the data the export step just wrote:
- the daily briefing, once a day from SOCIAL_BRIEFING_HOUR_UTC: the top headlines and a link card
  to /today (which also carries the audio version);
- breaking stories, at most one per run and SOCIAL_MAX_PER_DAY a day, using the same bar as the
  browser alerts: first published in the last few hours and covered by at least two outlets with
  real importance, or very important on its own. Each link card shows the article's own photo when it
  can be fetched, and falls back to our share image.

Every post is recorded in social_posts, so nothing is posted twice. SOCIAL_DRY_RUN=1 prints the
posts instead of publishing them. Failures are logged and never stop the pipeline.
"""
from __future__ import annotations

import json
import logging
import io
import re
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image
from sqlalchemy import func, insert, select

from . import config, db

log = logging.getLogger("digest.social")

API = "https://bsky.social/xrpc"
TEXT_LIMIT = 290  # Bluesky allows 300 graphemes; keep a margin
FRESH_HOURS = 6
UTM = "utm_source=bluesky&utm_medium=social"
THUMB_MAX_BYTES = 950_000  # Bluesky rejects blobs over 1 MB
THUMB_SIZE = (1200, 630)


def _photo(url: str | None) -> bytes | None:
    """The article's own photo as a JPEG small enough for a Bluesky link card, or None."""
    if not url or not url.startswith("http"):
        return None
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": config.USER_AGENT})
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/") or len(r.content) > 15_000_000:
            return None
        img = Image.open(io.BytesIO(r.content))
        if img.width < 400 or img.height < 200:
            return None  # logos and icons look worse than our share card
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE)
        for quality in (85, 75, 65, 55):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            if buf.tell() <= THUMB_MAX_BYTES:
                return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the share card
        log.info("article photo unavailable (%s): %s", url[:80], str(exc)[:80])
    return None


def _clip(text: str, limit: int) -> str:
    """Shorten to at most `limit` characters, cutting at a word boundary."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def _first_sentence(text: str, limit: int = 220) -> str:
    plain = re.sub(r"[*_`#>\[\]]", "", text or "")
    plain = re.sub(r"\(https?://[^)]*\)", "", plain)
    m = re.match(r"(.+?[.!?])(\s|$)", plain.strip())
    return _clip(m.group(1) if m else plain, limit)


def _tag_facets(text: str, tags: list[str]) -> list[dict]:
    """Hashtag facets use UTF-8 byte offsets."""
    facets = []
    raw = text.encode("utf-8")
    for tag in tags:
        needle = f"#{tag}".encode("utf-8")
        start = raw.rfind(needle)
        if start >= 0:
            facets.append({"index": {"byteStart": start, "byteEnd": start + len(needle)},
                           "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}]})
    return facets


# Post format follows what performs for news accounts on Bluesky. A study of 3,748 posts from 21 news
# and tech accounts (September 2026, engagement relative to each account's median) found: the news
# stated plainly in a full sentence does best, and 240+ characters beats short posts; link cards are
# the norm and links are not demoted; hashtags, questions and numbered lists add nothing or hurt; and
# 18:00-21:00 UTC is the strongest window.


_SOURCE_VOICE = re.compile(r"\b(the author|the writer|this (article|post|piece|essay|newsletter|video)|in this|I|I'm|I've|we|we're|our|my)\b", re.I)


def _lede(story: dict, limit: int = 280) -> str:
    """The news itself: the digest's opening sentence, or the headline plus its first key point.

    The opening sentence is used only when it is a full news sentence of at least 140 characters and
    does not speak in the source writer's voice ("the author", "I", "this article")."""
    first = _first_sentence(story.get("summaryMd") or "", limit)
    if 140 <= len(first) <= limit and not first.endswith("…") and not _SOURCE_VOICE.search(first):
        return first
    head = _clip(story["headline"], 140).rstrip(".")
    points = story.get("keyPoints") or []
    return _clip(f"{head}. {points[0]}", limit) if points else head


def _card_description(story: dict, text: str) -> str:
    """Something the post text does not already say: why it matters, else another key point."""
    for candidate in [story.get("whyItMatters") or "", *(story.get("keyPoints") or [])]:
        candidate = _clip(candidate, 280)
        if candidate and candidate[:40] not in text:
            return candidate
    return _clip(story["headline"], 280)


def _story_post(story: dict) -> dict:
    text = _lede(story, TEXT_LIMIT)
    return {
        "kind": "story",
        "key": story["slug"],
        "text": text,
        "tags": [],
        "link": f"{config.SITE_URL}/story/{story['slug']}?{UTM}",
        "title": _clip(story["headline"], 200),
        "description": _card_description(story, text),
        "photo": story.get("imageUrl"),
        "image": config.ROOT / "site" / "public" / "og" / f"{story['slug']}.png",
    }


def _briefing_post(briefing: dict, by_id: dict, date_label: str) -> dict | None:
    """Evening recap: lead with the day's biggest story in a sentence, point to the rest."""
    top = [by_id[i] for i in briefing.get("storyIds", []) if i in by_id]
    if len(top) < 3:
        return None
    minutes = max(3, (briefing.get("stats") or {}).get("minutes", 5))
    tail = f"\n\nPlus {len(top) - 1} more stories that mattered today, each with its sources, and a {minutes}-minute listen."
    intro = "Today in AI: "
    text = intro + _lede(top[0], TEXT_LIMIT - len(tail) - len(intro)) + tail
    return {
        "kind": "briefing",
        "key": briefing["date"],
        "text": text,
        "tags": [],
        "link": f"{config.SITE_URL}/today?{UTM}",
        "title": f"Today's AI briefing · {date_label}",
        "description": " · ".join(_clip(s["headline"], 90) for s in top[1:4]),
        "photo": top[0].get("imageUrl"),
        "image": config.ROOT / "site" / "public" / "og-default.png",
    }


class Bluesky:
    def __init__(self, handle: str, password: str):
        r = requests.post(f"{API}/com.atproto.server.createSession", json={"identifier": handle, "password": password}, timeout=30)
        r.raise_for_status()
        data = r.json()
        self.did = data["did"]
        self.headers = {"Authorization": f"Bearer {data['accessJwt']}"}

    def upload(self, raw: bytes | None, mime: str) -> dict | None:
        if not raw or len(raw) > THUMB_MAX_BYTES:
            return None
        r = requests.post(f"{API}/com.atproto.repo.uploadBlob", data=raw,
                          headers={**self.headers, "Content-Type": mime}, timeout=60)
        return r.json().get("blob") if r.status_code == 200 else None

    def thumb(self, item: dict) -> dict | None:
        """The article's photo first, then our share image."""
        blob = self.upload(_photo(item.get("photo")), "image/jpeg")
        if blob:
            return blob
        try:
            return self.upload(item["image"].read_bytes(), "image/png")
        except OSError:
            return None

    def post(self, item: dict) -> str:
        external = {"uri": item["link"], "title": item["title"], "description": item["description"]}
        thumb = self.thumb(item)
        if thumb:
            external["thumb"] = thumb
        record = {
            "$type": "app.bsky.feed.post",
            "text": item["text"],
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
            "facets": _tag_facets(item["text"], item["tags"]),
            "embed": {"$type": "app.bsky.embed.external", "external": external},
        }
        r = requests.post(f"{API}/com.atproto.repo.createRecord", headers=self.headers, timeout=30,
                          json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record})
        r.raise_for_status()
        return r.json().get("uri", "")


def _plan(now: datetime) -> list[dict]:
    data = config.SITE_DATA_DIR
    try:
        stories = json.loads((data / "stories.json").read_text(encoding="utf-8"))
        briefing = json.loads((data / "briefing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    by_id = {s["id"]: s for s in stories}
    eng = db.engine()
    with eng.connect() as conn:
        done = {(r.kind, r.key) for r in conn.execute(select(db.social_posts.c.kind, db.social_posts.c.key)
                                                        .where(db.social_posts.c.network == "bluesky")).all()}
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stories_today = conn.execute(select(func.count()).select_from(db.social_posts).where(
            db.social_posts.c.network == "bluesky", db.social_posts.c.kind == "story",
            db.social_posts.c.created_at >= day_start)).scalar() or 0

    plan: list[dict] = []
    today = now.date().isoformat()
    if now.hour >= config.SOCIAL_BRIEFING_HOUR_UTC and briefing.get("date") == today and ("briefing", today) not in done:
        label = now.strftime("%a %d %b").replace(" 0", " ")
        item = _briefing_post(briefing, by_id, label)
        if item:
            plan.append(item)

    if stories_today < config.SOCIAL_MAX_PER_DAY:
        cutoff = (now - timedelta(hours=FRESH_HOURS)).isoformat().replace("+00:00", "Z")
        fresh = [
            s for s in stories
            if (s.get("firstPublishedAt") or "") >= cutoff and ("story", s["slug"]) not in done
            and ((s.get("articleCount", 1) >= 2 and (s.get("importance") or 0) >= 6) or (s.get("importance") or 0) >= 8)
        ]
        fresh.sort(key=lambda s: -(s.get("score") or 0))
        if fresh:
            plan.append(_story_post(fresh[0]))
    return plan


def run() -> dict:
    stats = {"posted": 0, "planned": 0, "skipped": ""}
    dry = config.SOCIAL_DRY_RUN
    if not dry and not (config.BLUESKY_HANDLE and config.BLUESKY_APP_PASSWORD):
        stats["skipped"] = "no Bluesky credentials"
        return stats
    now = datetime.now(timezone.utc)
    plan = _plan(now)
    stats["planned"] = len(plan)
    if not plan:
        stats["skipped"] = "nothing to post"
        return stats
    if dry:
        for item in plan:
            log.info("DRY RUN %s post (%d chars) -> %s\n%s", item["kind"], len(item["text"]), item["link"], item["text"])
        stats["skipped"] = "dry run"
        return stats
    try:
        client = Bluesky(config.BLUESKY_HANDLE, config.BLUESKY_APP_PASSWORD)
    except Exception as exc:  # noqa: BLE001
        log.warning("Bluesky login failed: %s", str(exc)[:160])
        stats["skipped"] = "login failed"
        return stats
    eng = db.engine()
    for item in plan:
        try:
            uri = client.post(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bluesky %s post failed: %s", item["kind"], str(exc)[:160])
            continue
        with eng.begin() as conn:
            conn.execute(insert(db.social_posts).values(network="bluesky", kind=item["kind"], key=item["key"],
                                                        uri=uri, created_at=db.utcnow()))
        stats["posted"] += 1
        log.info("posted %s to Bluesky: %s", item["kind"], item["key"])
    return stats
