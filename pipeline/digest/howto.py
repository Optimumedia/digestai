"""Step: setup steps for AI at Work cards from the maker's own page.

A card gets "steps" from the article only when the article says how (enrich.py, work.ground_card).
Most launch coverage does not, but the maker's own page often does. For a card whose article gave no
steps and whose official link is on the maker's own domain, this step fetches that page once, asks
the model for 2-5 plain setup steps taken from it, and keeps them only when the page supports every
one, by the same rules the article's steps pass (work.step_grounded): each shares meaningful words
with the page, any menu path is written there, and no figure is one the page lacks. The card then
records where its steps came from (steps_source "maker"), and the site says so ("From Google's own
page").

What holds it in check:
- it runs after the export (run.ORDER), so a slow page never delays the hourly site update; steps
  found here reach the site with the next run's export;
- the cards come from the export the run just wrote (site/src/data/stories.json), and the stored card
  it adds steps to from the runner's copy of the rows (cache.py): no extra database reads;
- at most MAX_FETCHES pages a run, inside TIME_BUDGET_SECONDS, and each address once: what a page
  gave (steps, none, or a failure) is kept in the runner cache and reused, a failure retried after
  RETRY_DAYS;
- never a forum, a code host, a social network or a news site: only the maker's own domain;
- any failure just leaves the card without steps.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
from datetime import datetime, timezone

from . import checks, config, work

log = logging.getLogger("digest.howto")

MAX_FETCHES = 6              # maker pages fetched (and model calls made) per run
TIME_BUDGET_SECONDS = 60
PAGE_WORDS = 2500            # page text sent to the model
STEP_MAX = 120               # characters: a step is one short instruction
STEPS_MIN, STEPS_MAX = 2, 5
RETRY_DAYS = 7               # a page that failed to load is tried again after this long
NONE_DAYS = 30               # a page that said nothing about setting up is asked again after this long
KEEP_DAYS = 90               # cache entries older than this are dropped
CACHE_FILE = config.CACHE_DIR / "howto.json.gz"

# Publishers: a "try it" link to a news site is coverage, not the maker's page. (The maker check
# below already requires the maker's own domain; these are refused even if a name happens to match.)
NEWS_HOSTS = (
    "techcrunch.com", "theverge.com", "wired.com", "venturebeat.com", "zdnet.com", "cnet.com", "engadget.com",
    "reuters.com", "bloomberg.com", "nytimes.com", "wsj.com", "ft.com", "theguardian.com", "bbc.co.uk", "bbc.com",
    "cnbc.com", "forbes.com", "businessinsider.com", "axios.com", "arstechnica.com", "9to5google.com", "9to5mac.com",
    "macrumors.com", "searchengineland.com", "searchenginejournal.com", "socialmediaexaminer.com", "martech.org",
    "smallbiztrends.com", "marketingaiinstitute.com", "thenextweb.com", "tomsguide.com", "techradar.com",
    "androidauthority.com", "androidpolice.com", "theinformation.com", "semafor.com", "fastcompany.com",
    "inc.com", "entrepreneur.com", "medium.com", "substack.com", "news.google.com", "producthunt.com",
    "youtube.com", "youtu.be", "linkedin.com", "facebook.com", "instagram.com", "threads.net", "tiktok.com",
)
# Words in a maker's name that say nothing about which domain is theirs.
MAKER_FILLER = {"the", "inc", "llc", "ltd", "corp", "corporation", "company", "co", "labs", "lab", "ai", "app",
                "apps", "hq", "technologies", "technology", "software", "group", "platforms", "systems", "pbc"}

PROMPT = """Below is the text of {maker}'s own page about {tool}. A small-business owner who is not technical wants to start using it.
Return ONLY a JSON object: {{"steps": [...]}} with 2-5 setup steps taken from this page, in order. Each step is one plain instruction under 120 characters, like "Open Gmail on your computer, click Settings, turn on Help me write".
Use only what the page says: its button and menu names as it writes them, no number it does not give, nothing from anywhere else. No jargon. If the page does not say how to start using it, return {{"steps": []}}.

Page text:
\"\"\"
{text}
\"\"\"
"""


# ---------------------------------------------------------------------------- which links

def _maker_name(maker: str) -> str:
    name = re.sub(r"[^a-z0-9 ]+", " ", (maker or "").lower())
    return re.sub(r"\s+", " ", name).strip()


def on_maker_domain(maker: str | None, link: str | None) -> bool:
    """The link is on the maker's own domain: a maker the rules know (work.MAKER_DOMAINS) on one of
    its domains, else a host whose name is the maker's ("Klaviyo" on help.klaviyo.com)."""
    host = work._host(link)
    name = _maker_name(maker or "")
    # A maker that is a web address is one work.check_maker took from a link that did not match the
    # company the card named: a third party's page, not the maker's.
    if not host or not name or "." in (maker or ""):
        return False
    for _display, names, domains in work.MAKER_DOMAINS.values():
        if name in names:
            return work._on(host, domains)
    labels = [lab for lab in host.split(".")[:-1] if lab not in ("www", "com", "co")]
    squashed = re.sub(r"[^a-z0-9]", "", name)
    tokens = [t for t in name.split() if len(t) >= 4 and t not in MAKER_FILLER]
    for lab in labels:
        parts = {lab.replace("-", ""), *lab.split("-")}
        if len(squashed) >= 4 and squashed in parts:
            return True
        if any(t in parts for t in tokens):
            return True
    return False


def fetchable(card: dict, story: dict | None = None) -> str | None:
    """Why a card's link is not to be fetched, or None when it is the maker's own page.

    `card` is the exported shape (work.card_out). Never a forum, a code host or a social network
    (work.COMMUNITY_HOSTS), never a news site (NEWS_HOSTS, or the domain of any press, newsletter or
    community source covering the story), and only a link on the maker's own domain."""
    link = card.get("link") or ""
    host = work._host(link)
    if not host or not link.startswith("https://"):
        return "no link"
    if work._on(host, work.COMMUNITY_HOSTS):
        return "community"
    if work._on(host, NEWS_HOSTS):
        return "news"
    for a in (story or {}).get("articles") or []:
        domain = (a.get("domain") or work._host(a.get("url"))).removeprefix("www.")
        if domain and a.get("sourceType") != "primary" and work._on(host, (domain,)):
            return "news"
    if not on_maker_domain(card.get("maker"), link):
        return "not the maker"
    return None


# ---------------------------------------------------------------------------- the steps

def ground_steps(steps, page: str) -> list[str]:
    """2-5 plain steps the page supports, or none: every step shares meaningful words with the page,
    names only menu paths the page writes, uses no figure the page lacks, has no jargon, and is at
    most STEP_MAX characters. One step that fails drops them all: half a list teaches the wrong thing."""
    if len(page or "") < checks.MIN_SOURCE_CHARS:
        return []
    out: list[str] = []
    for s in work._list(steps):
        s = re.sub(r"^(?:step\s*)?\d{1,2}[.):]\s*", "", work._text(s, 400), flags=re.I).strip()
        if not s or work._empty(s):
            continue
        if s.lower() not in {x.lower() for x in out}:
            out.append(s)
    if not STEPS_MIN <= len(out) <= STEPS_MAX:
        return []
    words, flat = work._content_words(page), re.sub(r"[^a-z0-9]+", "", page.lower())
    for s in out:
        if len(s) > STEP_MAX or "!" in s or work.YOU_GET_BANNED.search(work._dashes(s)):
            return []
        if not work.step_grounded(s, words, flat, page):
            return []
    return out


def page_text(url: str) -> str | None:
    """The page's main text (trafilatura, with lists kept), or None when it cannot be read."""
    from . import extract  # trafilatura is loaded by the steps that read pages, not at import

    html, err = extract.fetch_html(url)
    if not html:
        log.info("howto: %s not fetched (%s)", url, err)
        return None
    md = extract._markdown_from_html(html, url, precision=False) or extract._markdown_from_html(html, url)
    return extract.to_text(md) if md else None


def ask_model(prompt: str) -> dict:
    """Gemini, then Groq, then Ollama Cloud, then Cloudflare and Mistral (which refuse by themselves
    once their daily neurons or monthly spend, or the run's call cap, are used); an exception when
    none is configured or all refuse."""
    from . import enrich

    errors = []
    cloudflare = config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_AI_TOKEN
    for key, call in ((config.GEMINI_API_KEY, enrich.call_gemini), (config.GROQ_API_KEY, enrich.call_groq),
                      (config.OLLAMA_API_KEY, enrich.call_ollama_cloud), (cloudflare, enrich.call_cloudflare),
                      (config.MISTRAL_API_KEY, enrich.call_mistral)):
        if not key:
            continue
        try:
            return call(prompt)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc.__class__.__name__)
    raise RuntimeError("no model answered: " + (",".join(errors) or "no key configured"))


def steps_for(card: dict, fetch=page_text, ask=ask_model) -> tuple[list[str], str]:
    """(steps, status) for one card's maker page: status "ok", "none" (the page gives no grounded
    steps) or "failed" (the page could not be read, or no model answered)."""
    text = fetch(card["link"])
    if not text or len(text) < checks.MIN_SOURCE_CHARS:
        return [], "failed"
    text = " ".join(text.split()[:PAGE_WORDS])
    try:
        answer = ask(PROMPT.format(maker=card.get("maker") or "the maker", tool=card["tool"], text=text))
    except Exception as exc:  # noqa: BLE001
        log.info("howto: no answer for %s (%s)", card["link"], exc)
        return [], "failed"
    steps = ground_steps((answer or {}).get("steps"), text)
    return steps, ("ok" if steps else "none")


# ---------------------------------------------------------------------------- the cache

def load_cache() -> dict:
    try:
        with gzip.open(CACHE_FILE, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, EOFError):
        return {}


def save_cache(data: dict, now: float | None = None) -> None:
    """Saved with entries younger than KEEP_DAYS: a card leaves the export window long before."""
    cutoff = (now if now is not None else time.time()) - KEEP_DAYS * 86400
    data = {k: v for k, v in data.items() if float((v or {}).get("at") or 0) >= cutoff}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(CACHE_FILE, "wt", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:
        log.warning("howto: cache not saved (%s)", exc)


def _due(entry: dict | None, now: float) -> bool:
    """A page to fetch: never seen, or a failure or a "none" old enough to ask again."""
    if not entry:
        return True
    age = now - float(entry.get("at") or 0)
    if entry.get("status") == "ok":
        return False
    return age > (RETRY_DAYS if entry.get("status") == "failed" else NONE_DAYS) * 86400


# ---------------------------------------------------------------------------- the step

def candidates(stories: list[dict]) -> list[tuple[dict, dict, list[int]]]:
    """(story, card, article ids) for exported cards with no steps and a maker page to read, newest
    first. The article ids are those whose own card is the story's (same tool and link)."""
    out = []
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or "", reverse=True):
        card = s.get("workCard")
        if not card or card.get("steps") or fetchable(card, s):
            continue
        ids = [a["id"] for a in s.get("articles") or []
               if (a.get("workCard") or {}).get("tool") == card["tool"] and (a.get("workCard") or {}).get("link") == card["link"]]
        if ids:
            out.append((s, card, ids))
    return out


def store_steps(updates: dict[int, list[str]]) -> int:
    """Add the steps to the stored cards of these articles (only to a card that still has none).
    The cards are read from the runner's copy of the rows, which the export just brought up to date."""
    from sqlalchemy import update

    from . import cache, db

    written = 0
    eng = db.engine()
    with eng.begin() as conn:
        mirror = cache.articles(conn)
        rows = [mirror[i] for i in updates if i in mirror]
        texts = cache.article_text(conn, rows)
        for i, steps in updates.items():
            t = texts.get(i)
            card = dict(t.work_card) if t is not None and isinstance(t.work_card, dict) else None
            if not card or card.get("steps"):
                continue
            card["steps"], card["steps_source"] = list(steps), "maker"
            conn.execute(update(db.articles).where(db.articles.c.id == i).values(work_card=card))
            written += 1
        cache.forget_details(conn, cache.ARTICLE_TEXT, list(updates))
    return written


def run(stories: list[dict] | None = None, fetch=page_text, ask=ask_model, store=store_steps,
        now: float | None = None, limit: int = MAX_FETCHES, budget_seconds: float = TIME_BUDGET_SECONDS,
        cache_data: dict | None = None) -> dict:
    stats = {"candidates": 0, "fetched": 0, "found": 0, "none": 0, "failed": 0, "reused": 0, "written": 0}
    if stories is None:
        path = config.SITE_DATA_DIR / "stories.json"
        try:
            stories = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {**stats, "skipped": "no export"}
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    own_cache = cache_data is None
    data = load_cache() if own_cache else cache_data
    t0 = time.time()
    updates: dict[int, list[str]] = {}
    todo = candidates(stories)
    stats["candidates"] = len(todo)
    for _story, card, ids in todo:
        url = card["link"]
        entry = data.get(url)
        if entry and entry.get("status") == "ok" and entry.get("steps"):
            for i in ids:
                updates[i] = entry["steps"]
            stats["reused"] += 1
            continue
        if not _due(entry, now):
            continue
        if stats["fetched"] >= limit or time.time() - t0 > budget_seconds:
            break
        stats["fetched"] += 1
        steps, status = steps_for(card, fetch=fetch, ask=ask)
        data[url] = {"at": now, "status": status, "steps": steps}
        stats["found" if status == "ok" else status] += 1
        if steps:
            for i in ids:
                updates[i] = steps
    if own_cache:
        save_cache(data, now)
    if updates:
        try:
            stats["written"] = store(updates)
        except Exception as exc:  # noqa: BLE001
            log.warning("howto: steps not stored (%s)", exc)
            stats["storeFailed"] = True
    return stats
