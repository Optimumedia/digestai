"""Content quality monitor: checks what readers actually see, every run, and writes plain-English
flags into admin.json (called from the admin step).

The rules are small pure functions over the exported data files (stories.json, briefing.json,
trackers.json, episodes.json), so they are easy to test. The only network work is a bounded set
of quick image and link checks with short timeouts and an overall deadline of a few seconds.

Each flag becomes an action card on the dashboard: what happened, why it matters, what to do,
and at most one action (unpublish a story, or a link)."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Callable

from . import config

log = logging.getLogger("digest.quality")

LIVE_HOURS = 48            # a story first published this recently is shown as news
OLD_DAYS = 10              # an article this much older than the story's first publication is old news
URL_DATE_DAYS = 62         # a /2024/03/ style link path this much older is old news
MAX_SOURCES = 40           # more sources than this means unrelated articles were merged
MAX_IMAGES = 30            # image checks per run
MAX_LINKS = 8              # source-link checks per run (front-page stories)
MAX_STORY_PAGES = 6        # checks of our own older story pages
TIMEOUT = (3, 4)           # connect, read seconds per request
DEADLINE = 8.0             # all network checks together
MAX_ITEMS = 8              # items listed on one card

STOP = set("a an the and or of to in on for with by at from as is are be its it this that new says after over into how why what".split())
YEAR_MARK = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]")
URL_DATE = re.compile(r"/((?:19|20)\d{2})/(0[1-9]|1[0-2])(?:/|$)")


def _dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _lead(story: dict) -> dict:
    arts = story.get("articles") or []
    return next((a for a in arts if a.get("isLead")), arts[0] if arts else {})


def _item(story: dict, detail: str, action: dict | None = None) -> dict:
    return {"slug": story.get("slug"), "headline": story.get("headline") or story.get("slug"), "detail": detail,
            **({"action": action} if action else {})}


def _unpublish(story: dict) -> dict:
    return {"kind": "unpublish", "slug": story.get("slug"), "label": "Unpublish"}


def live_stories(stories: list[dict], now: datetime) -> list[dict]:
    cut = now - timedelta(hours=LIVE_HOURS)
    return [s for s in stories if (_dt(s.get("firstPublishedAt")) or cut) > cut]


# ---------------------------------------------------------------------------- rules

def year_marker(title: str | None, now: datetime) -> int | None:
    """A past year in brackets, like "Language Models are Unsupervised Multitask Learners (2019)"."""
    for m in YEAR_MARK.finditer(title or ""):
        year = int(m.group(1))
        if year < now.year:
            return year
    return None


def url_date(url: str | None) -> datetime | None:
    m = URL_DATE.search(url or "")
    return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc) if m else None


def old_news(stories: list[dict], now: datetime) -> list[dict]:
    """Live stories whose material is much older than the moment we first published them."""
    out = []
    for s in live_stories(stories, now):
        first = _dt(s.get("firstPublishedAt")) or now
        arts = s.get("articles") or []
        reason = None
        year = year_marker(s.get("headline"), now) or next((y for a in arts if (y := year_marker(a.get("title"), now))), None)
        if year:
            reason = f"the title says ({year})"
        if not reason:
            dates = [d for a in arts if (d := _dt(a.get("publishedAt")))]
            # Every article old, or the lead old: one fresh follow-up does not make old news new.
            lead_date = _dt(_lead(s).get("publishedAt"))
            newest = max(dates) if dates else None
            if newest and newest < first - timedelta(days=OLD_DAYS):
                reason = f"its articles are from {newest:%d %b %Y}"
            elif lead_date and lead_date < first - timedelta(days=OLD_DAYS):
                reason = f"the main article is from {lead_date:%d %b %Y}"
        if not reason:
            ud = url_date(_lead(s).get("url"))
            if ud and ud < first - timedelta(days=URL_DATE_DAYS):
                reason = f"the article link is dated {ud:%b %Y}"
        if reason:
            out.append(_item(s, f"Shown as new, but {reason}.", _unpublish(s)))
    return out


def single_source_lead(briefing: dict, by_id: dict[int, dict]) -> dict | None:
    ids = briefing.get("storyIds") or []
    lead = by_id.get(ids[0]) if ids else None
    if lead and (lead.get("articleCount") or len(lead.get("articles") or [])) <= 1 and not lead.get("pinned"):
        src = _lead(lead).get("source") or _lead(lead).get("domain") or "one source"
        return _item(lead, f"Only {src} has reported it so far.", _unpublish(lead))
    return None


def over_merged(stories: list[dict]) -> list[dict]:
    rows = [s for s in stories if (s.get("articleCount") or 0) > MAX_SOURCES]
    rows.sort(key=lambda s: -(s.get("articleCount") or 0))
    return [_item(s, f"{s['articleCount']} sources grouped into one story.", _unpublish(s)) for s in rows]


def _tokens(text: str | None) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split() if t not in STOP]


def headline_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    if jac < 0.4:
        return jac
    return max(jac, SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio())


def duplicates(stories: list[dict], now: datetime, threshold: float = 0.8) -> list[dict]:
    """Near-identical headlines among live stories. The copy with fewer sources is the one to
    unpublish; the other is named in the detail."""
    live = sorted(live_stories(stories, now), key=lambda s: (-(s.get("articleCount") or 0), s.get("firstPublishedAt") or ""))
    toks = [set(_tokens(s.get("headline"))) for s in live]
    out, used = [], set()
    for i, keep in enumerate(live):
        if keep.get("slug") in used:
            continue
        for j in range(i + 1, len(live)):
            dup = live[j]
            if dup.get("slug") in used or len(toks[i]) < 3 or len(toks[j]) < 3:
                continue
            if len(toks[i] & toks[j]) / len(toks[i] | toks[j]) < 0.4:
                continue
            if headline_similarity(keep.get("headline"), dup.get("headline")) >= threshold:
                used.add(dup.get("slug"))
                out.append(_item(dup, f"Same news as \"{keep.get('headline')}\" ({keep.get('articleCount') or 1} sources).", _unpublish(dup)))
    return out


def tracker_gaps(trackers: dict) -> list[dict]:
    """Model rows without a lab, funding rows without a company. "Other" is a real type (weather,
    forecasting and science models; unusual deal types), so it is not a gap."""
    out = []
    unknown = lambda v: not v or str(v).strip().lower() in ("unknown", "n/a", "none")  # noqa: E731
    for r in trackers.get("models") or []:
        gaps = ["lab unknown"] if unknown(r.get("lab")) else []
        if gaps:
            out.append({"slug": r.get("storySlug"), "headline": r.get("name"), "detail": f"Models page: {', '.join(gaps)}.", "page": "/models"})
    for r in trackers.get("funding") or []:
        gaps = ["company unknown"] if unknown(r.get("company")) else []
        if gaps:
            out.append({"slug": r.get("storySlug"), "headline": r.get("company") or "Unnamed company", "detail": f"Funding page: {', '.join(gaps)}.", "page": "/funding"})
    return out


def audio_mismatch(episodes: list[dict], briefing: dict, by_id: dict[int, dict]) -> dict | None:
    ep = next((e for e in episodes or [] if e.get("date") == briefing.get("date")), None)
    ids = briefing.get("storyIds") or []
    lead = by_id.get(ids[0]) if ids else None
    first = ((ep or {}).get("stories") or [{}])[0]
    if not ep or not lead or not first.get("slug") or first["slug"] == lead.get("slug"):
        return None
    return {"slug": first["slug"], "headline": first.get("headline") or first["slug"],
            "detail": f"The audio opens with this story, while /today opens with \"{lead.get('headline')}\"."}


# ---------------------------------------------------------------------------- network checks

def _default_fetch(url: str) -> int | None:
    import requests

    headers = {"User-Agent": getattr(config, "BROWSER_AGENT", config.USER_AGENT), "Accept": "*/*"}
    try:
        r = requests.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (400, 403, 405, 501):
            # Some servers refuse HEAD but serve GET; confirm before calling it broken.
            r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True)
            r.close()
        return r.status_code
    except Exception:  # noqa: BLE001  (timeouts and connection errors are "unknown", not broken)
        return None


def check_urls(urls: list[str], fetch: Callable[[str], int | None] | None = None, deadline: float = DEADLINE) -> dict[str, int | None]:
    """Status per URL; URLs not answered before the deadline are left out."""
    fetch = fetch or _default_fetch
    urls = list(dict.fromkeys(u for u in urls if u and u.startswith("http")))
    if not urls:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(10, len(urls)))
    futures = {pool.submit(fetch, u): u for u in urls}
    done, _ = wait(futures, timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    return {futures[f]: f.result() for f in done if not f.exception()}


def front_page(stories: list[dict], briefing: dict, by_id: dict[int, dict]) -> list[dict]:
    """The stories on the home page and in the briefing, in page order."""
    ids = list(briefing.get("storyIds") or []) + list(briefing.get("alsoIds") or [])
    seen = set(ids)
    top = [by_id[i] for i in ids if i in by_id]
    recent = sorted(stories, key=lambda s: s.get("updatedAt") or s.get("firstPublishedAt") or "", reverse=True)
    return top + [s for s in recent if s.get("id") not in seen][:20]


def broken_images(page: list[dict], statuses: dict[str, int | None]) -> list[dict]:
    out = []
    for s in page:
        url = s.get("imageUrl")
        code = statuses.get(url) if url else None
        if code and code >= 400:
            host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
            out.append(_item(s, f"Its picture from {host} does not load (error {code}). The page hides it, so the story shows without a picture."))
    return out


def broken_links(page: list[dict], statuses: dict[str, int | None], own_pages: list[dict]) -> list[dict]:
    out = []
    for s in page:
        url = _lead(s).get("url")
        if url and statuses.get(url) in (404, 410):
            out.append(_item(s, f"The original article ({_lead(s).get('source') or _lead(s).get('domain')}) now shows \"page not found\".", _unpublish(s)))
    for s in own_pages:
        url = f"{config.SITE_URL}/story/{s['slug']}"
        if statuses.get(url) in (404, 410):
            out.append({"slug": s["slug"], "headline": s.get("headline") or s["slug"], "detail": "Our own story page now shows \"page not found\"; links people shared to it are broken."})
    return out


# ---------------------------------------------------------------------------- cards

def cards(flags: dict[str, list[dict]]) -> list[dict]:
    """Plain-English action cards from the flag lists. Empty lists produce nothing."""
    out = []

    def add(key, level, what, why, todo, items, action=None):
        if items:
            out.append({"id": f"quality:{key}", "level": level, "what": what, "why": why, "todo": todo,
                        "items": items[:MAX_ITEMS], "more": max(0, len(items) - MAX_ITEMS), **({"action": action} if action else {})})

    n = lambda items, one, many: one if len(items) == 1 else many.format(n=len(items))  # noqa: E731
    f = flags
    add("old_news", "warning", n(f.get("oldNews", []), "An old story is on the site as new.", "{n} old stories are on the site as new."),
        "Readers trust a news site that shows today's news. An article from years ago presented as new makes the whole site look careless.",
        "Check the story. If it really is old, unpublish it (the button removes it on the next run).", f.get("oldNews", []))
    add("single_lead", "info", "The briefing leads with a story only one source has reported.",
        "The first story sets the tone of the day. A single blog post may be minor or wrong, and nobody else has confirmed it yet.",
        "Usually nothing: the order changes as more sources arrive. If it is still first in a few hours and does not deserve it, unpublish it.",
        [x for x in [f.get("singleLead")] if x], {"kind": "link", "url": "/today", "label": "Open /today"})
    add("over_merged", "warning", n(f.get("overMerged", []), "One story has grouped too many sources.", "{n} stories have grouped too many sources."),
        "More than 40 sources in one story almost always means unrelated articles were lumped together, so the summary can mix up different news.",
        "Open the story. If the headline and summary do not match the sources, unpublish it; the grouping rule needs tightening.", f.get("overMerged", []))
    add("duplicates", "warning", n(f.get("duplicates", []), "The same news appears twice.", "{n} stories repeat news that is already on the site."),
        "Readers see the same headline twice on the front page, which looks like a mistake.",
        "Unpublish the copy listed here; the version with more sources stays.", f.get("duplicates", []))
    add("broken_images", "info", n(f.get("brokenImages", []), "A picture on the front page does not load.", "{n} pictures on the front page do not load."),
        "The page hides a broken picture, so readers see the story without one. It looks less polished but nothing is broken.",
        "No action needed. It is usually the publisher blocking other sites from showing its pictures.", f.get("brokenImages", []))
    add("broken_links", "warning", n(f.get("brokenLinks", []), "A story link has stopped working.", "{n} story links have stopped working."),
        "Readers who click through land on a \"page not found\" error, which feels like a dead end.",
        "Unpublish stories whose original article was removed. If our own pages are listed, tell whoever maintains the site: old story pages should stay online.", f.get("brokenLinks", []))
    gaps = f.get("trackerGaps", [])
    add("trackers", "info", n(gaps, "One row on the Models or Funding page is incomplete.", "{n} rows on the Models and Funding pages are incomplete."),
        "A model without its lab, or a deal without its company, looks unfinished to readers comparing them.",
        "Nothing urgent. If a row is plainly wrong, unpublish the story it came from; otherwise the next model update usually fills it in.",
        gaps, {"kind": "link", "url": gaps[0].get("page", "/models") if gaps else "/models", "label": "Open the page"})
    add("audio", "info", "The audio briefing starts with a different story than /today.",
        "Listeners and readers get a different top story. The audio is recorded once a day, while /today keeps updating as news arrives.",
        "No action needed; tomorrow's audio follows the briefing again. If the audio's first story was a mistake, unpublish it.",
        [x for x in [f.get("audio")] if x], {"kind": "link", "url": "/listen", "label": "Open the audio page"})
    return out


# ---------------------------------------------------------------------------- step

def _load(name: str, default):
    try:
        return json.loads((config.SITE_DATA_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run(now: datetime, own_pages: list[dict] | None = None, fetch: Callable[[str], int | None] | None = None,
        network: bool | None = None) -> dict:
    """Checks the exported data files. own_pages: older published stories whose pages on the live
    site should still exist (sampled by the caller from the database)."""
    t0 = time.time()
    stories = [s for s in _load("stories.json", []) if s.get("articles")]
    briefing = _load("briefing.json", {})
    by_id = {s.get("id"): s for s in stories}
    flags: dict = {
        "oldNews": old_news(stories, now),
        "singleLead": single_source_lead(briefing, by_id),
        "overMerged": over_merged(stories),
        "duplicates": duplicates(stories, now),
        "trackerGaps": tracker_gaps(_load("trackers.json", {})),
        "audio": audio_mismatch(_load("episodes.json", []), briefing, by_id),
        "brokenImages": [], "brokenLinks": [],
    }
    network = (os.environ.get("QUALITY_NETWORK", "1") != "0") if network is None else network
    checked = {"stories": len(stories), "images": 0, "links": 0, "answered": 0}
    if network and stories:
        page = front_page(stories, briefing, by_id)
        images = list(dict.fromkeys(s["imageUrl"] for s in page if s.get("imageUrl")))[:MAX_IMAGES]
        links = [u for u in dict.fromkeys(_lead(s).get("url") for s in page[:MAX_LINKS]) if u]
        own = (own_pages or [])[:MAX_STORY_PAGES]
        urls = images + links + [f"{config.SITE_URL}/story/{s['slug']}" for s in own]
        statuses = check_urls(urls, fetch)
        checked.update(images=len(images), links=len(links) + len(own), answered=sum(1 for v in statuses.values() if v))
        flags["brokenImages"] = broken_images(page, statuses)
        flags["brokenLinks"] = broken_links(page, statuses, own)
    iso = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"checkedAt": iso, "seconds": round(time.time() - t0, 1), "checked": checked, "flags": flags, "cards": cards(flags)}
