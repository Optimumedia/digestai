"""Step 2: fetch article pages and extract the main text (not the page)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape as html_unescape
from pathlib import Path

import requests
import trafilatura
import yaml
from bs4 import BeautifulSoup
from readability import Document
from sqlalchemy import select, update

from . import config, db
from .textutil import domain_of, keywords, word_count

log = logging.getLogger("digest.extract")

OVERRIDES_FILE = Path(__file__).with_name("overrides.yaml")
_OVERRIDES: dict | None = None

SESSION = requests.Session()

# Paragraphs that are never article text.
DROP_PATTERNS = re.compile(
    r"^\s*(\**)?("
    r"sponsored|advertisement|advertising|ad feedback|more for you|more from|related( articles| stories| posts| content)?|"
    r"recommended( for you| stories)?|you may also like|you might also like|trending( now)?|most (read|popular)|"
    r"read (more|next|also)|also read|see also|subscribe( now| today)?|sign (in|up)|log in|share( this)?|"
    r"follow us|newsletter|cookie|privacy policy|terms of (use|service)|all rights reserved|"
    r"copyright ©|©|image( credit)?s?:|photo( credit)?:|getty images|shutterstock|"
    r"click here|listen to this article|watch:|loading\.\.\.|comments?|leave a (reply|comment)|"
    r"tags?:|filed under|topics?:|source:|via |originally published|this article (was|first) appeared|"
    r"the post .* appeared first on|disclosure:|editor'?s note|updated?:|correction:|"
    r"table of contents|skip to (main )?content|open in app|download (the|our) app|"
    r"follow (us|[a-z' ]+ on (google news|x|twitter|facebook|linkedin|instagram))|"
    r"in partnership with|presented by|brought to you by|paid (content|post)|"
    r"get [a-z' ]+ (best )?(news|stories|reviews|analysis)[^.]*inbox|sign up (for|to)|"
    r"we'?re ending our live coverage|"
    r"[a-z0-9._-]+·\d+[hdm]"
    r")\b.*$",
    re.IGNORECASE,
)
INBOX_PROMO = re.compile(
    r"\b(straight to your inbox|in your inbox|add us as a preferred source|preferred source|"
    r"curated by|if you would like to submit a response|this is an edition of|"
    r"sign up (for|to) (the|our) newsletter|subscribe to (the|our) newsletter|"
    r"do you have information about this story|contact (us|the author)|"
    r"this story (was|has been) updated|have a tip\??|reach (me|us) (at|on)|"
    r"listen to (our|the) podcast|watch (our|the) video)\b",
    re.IGNORECASE,
)
# Bare metadata lines such as "Published", "Updated on 3 May", "- Published".
META_LINE = re.compile(r"^(published|updated|last updated|posted)(\s+(on|at|by|in)\b.*)?$", re.IGNORECASE)
# A heading after which everything is navigation or recirculation.
CUT_PATTERNS = re.compile(
    r"^\s*#*\s*(\**)?(related( articles| stories| coverage| posts)?|more (for you|from|stories|on|in)|"
    r"recommended( for you)?|you may also like|read (more|next)|also read|what to read next|"
    r"trending( now)?|most (read|popular)|top (picks|stories)|sponsored( content)?|"
    r"popular (in|on|stories)|latest (news|stories)|up next|next up|from our partners|"
    r"further reading|see also|in other news|around the web|more news|newsletter sign.?up|"
    r"share this (article|story)|about the author|comments)\b",
    re.IGNORECASE,
)
TICKER_LINE = re.compile(r"^[A-Z]{2,6}[▲▼]", re.MULTILINE)
JUNK_SHORT = re.compile(r"^[\W\d]*$")
SHORT_MIN_WORDS = 100  # accept a short post when every candidate agrees it is the whole article


@dataclass
class Extraction:
    ok: bool = False
    method: str = "none"
    markdown: str = ""
    text: str = ""
    title: str | None = None
    author: str | None = None
    date: str | None = None
    image: str | None = None
    words: int = 0
    reason: str | None = None
    notes: list[str] = field(default_factory=list)


def overrides() -> dict:
    global _OVERRIDES
    if _OVERRIDES is None:
        raw = yaml.safe_load(OVERRIDES_FILE.read_text(encoding="utf-8")) or {}
        _OVERRIDES = raw.get("domains", {}) or {}
    return _OVERRIDES


def domain_rule(url: str) -> dict:
    dom = domain_of(url)
    rules = overrides()
    parts = dom.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in rules:
            return rules[cand] or {}
    return {}


def fetch_html(url: str, agent: str | None = None) -> tuple[str | None, str | None]:
    headers = {
        "User-Agent": config.BROWSER_AGENT if agent == "browser" else config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    }
    try:
        resp = SESSION.get(url, headers=headers, timeout=config.FETCH_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return None, f"fetch error: {exc.__class__.__name__}"
    if resp.status_code >= 400:
        return None, f"http {resp.status_code}"
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "xml" not in ctype:
        return None, f"not html: {ctype[:40]}"
    if len(resp.content) > 6_000_000:
        return None, "page too large"
    resp.encoding = resp.apparent_encoding if not resp.encoding or resp.encoding.lower() == "iso-8859-1" else resp.encoding
    return resp.text, None


# ---------------------------------------------------------------- candidates

def _jsonld_article(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        queue = data if isinstance(data, list) else [data]
        while queue:
            node = queue.pop()
            if not isinstance(node, dict):
                continue
            if "@graph" in node:
                queue.extend(node["@graph"])
            types = node.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if any(t in ("NewsArticle", "Article", "BlogPosting", "TechArticle", "ReportageNewsArticle") for t in types):
                return node
    return None


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _markdown_from_html(html: str, url: str, precision: bool = True) -> str | None:
    try:
        return trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_links=False,
            favor_precision=precision,
            favor_recall=not precision,
            deduplicate=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("trafilatura failed: %s", exc)
        return None


def _readability_markdown(html: str, url: str) -> str | None:
    try:
        fragment = Document(html).summary(html_partial=True)
    except Exception:  # noqa: BLE001
        return None
    if not fragment:
        return None
    wrapped = f"<html><body><article>{fragment}</article></body></html>"
    return _markdown_from_html(wrapped, url, precision=False)


def _text_from_articlebody(body: str) -> str:
    body = re.sub(r"\r", "", body)
    paras = [p.strip() for p in re.split(r"\n{2,}|(?<=[.!?])\s{2,}", body) if p.strip()]
    return "\n\n".join(paras)


# ------------------------------------------------------------------ cleaning

def scrub(markdown: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    # Extractors occasionally leave HTML entities (&gt;, &amp;) in text output.
    markdown = html_unescape(markdown or "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", markdown) if p.strip()]
    total = len(paras)
    kept: list[str] = []
    for idx, p in enumerate(paras):
        plain = p.lstrip("#>*- ").strip()
        # Recirculation headings ("Related articles", "More for you") are a few words long;
        # a sentence that merely starts with "Related ..." is content.
        if idx > total * 0.4 and word_count(plain) <= 6 and CUT_PATTERNS.match(plain):
            notes.append(f"cut at '{plain[:40]}'")
            break
        if DROP_PATTERNS.match(plain) and word_count(plain) < 40:
            continue
        if INBOX_PROMO.search(plain) and word_count(plain) < 45:
            continue
        if META_LINE.match(plain) and word_count(plain) < 8:
            continue
        if TICKER_LINE.search(p):
            continue
        if word_count(plain) <= 3 and not p.startswith("#") and JUNK_SHORT.match(plain.replace("'", "")):
            continue
        kept.append(p)
    # Trailing short fragments (share buttons, bylines, single words).
    while kept and word_count(kept[-1].lstrip("#>*- ")) <= 4:
        kept.pop()
    return "\n\n".join(kept), notes


def _truncate_markdown(markdown: str, max_words: int) -> str:
    """Cut at a paragraph boundary near max_words and say so."""
    out, total = [], 0
    for p in markdown.split("\n\n"):
        n = word_count(p)
        if total + n > max_words and out:
            break
        out.append(p)
        total += n
    return "\n\n".join(out) + "\n\n*This document continues at the source.*"


def to_text(markdown: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", markdown, flags=re.MULTILINE)
    text = re.sub(r"[*_`>]+", "", text)
    return text.strip()


def validate(text: str, title: str, min_words: int, max_words: int, page_title: str | None = None) -> str | None:
    words = word_count(text)
    if words < min_words:
        return f"too short ({words} words)"
    if words > max_words:
        return f"too long ({words} words)"
    kws = keywords(title, 5)
    if kws:
        low = text.lower().replace("’", "'")
        hits = sum(1 for k in kws if k in low)
        need = 2 if len(kws) >= 3 else 1
        # When the page's own title matches the feed title we know we extracted the right page;
        # a roundup whose headline shares no words with its body is then still acceptable.
        if page_title and _same_title(page_title, title):
            need = min(need, 1) if hits else 0
        if hits < need:
            return f"title/body mismatch ({hits}/{len(kws)} keywords)"
    return None


def _same_title(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()  # noqa: E731
    na, nb = norm(a), norm(b)
    return bool(na and nb) and (na == nb or na.startswith(nb) or nb.startswith(na))


# --------------------------------------------------------------------- main

def extract(url: str, html: str | None, title: str, feed_content: str | None = None,
            content_from_feed: bool = False, min_words: int | None = None) -> Extraction:
    min_words = min_words if min_words is not None else config.MIN_WORDS
    result = Extraction()
    rule = domain_rule(url)

    # 1. Trust the feed body when the source says so, or when it is a full article.
    if feed_content and (content_from_feed or word_count(feed_content) >= min_words):
        md = _markdown_from_html(f"<html><body><article>{feed_content}</article></body></html>", url, precision=False)
        if md:
            md, notes = scrub(md)
            text = to_text(md)
            err = validate(text, title, 40 if content_from_feed else min_words, config.MAX_WORDS)
            if not err:
                result.ok, result.method, result.markdown, result.text = True, "feed", md, text
                result.words, result.notes = word_count(text), notes
                if content_from_feed or not html:
                    return result

    if not html:
        if not result.ok:
            result.reason = "no html"
        return result

    soup = BeautifulSoup(html, "lxml")
    result.image = _meta(soup, "og:image", "twitter:image")
    result.title = _meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    result.author = _meta(soup, "author", "article:author", "parsely-author")
    result.date = _meta(soup, "article:published_time", "datePublished", "parsely-pub-date")

    candidates: list[tuple[str, str]] = []

    # 2. Structured data: JSON-LD articleBody is never polluted by sidebars.
    node = _jsonld_article(soup)
    if node:
        body = node.get("articleBody")
        if isinstance(body, str) and word_count(body) >= min_words:
            candidates.append(("jsonld", _text_from_articlebody(body)))
        if not result.author and node.get("author"):
            a = node["author"]
            a = a[0] if isinstance(a, list) and a else a
            if isinstance(a, dict) and a.get("name"):
                result.author = str(a["name"])[:300]
        result.date = result.date or node.get("datePublished")
        img = node.get("image")
        if not result.image and img:
            img = img[0] if isinstance(img, list) and img else img
            result.image = img.get("url") if isinstance(img, dict) else (img if isinstance(img, str) else None)

    # 3. Optional container isolation for known-difficult publishers.
    scoped_html = html
    if rule.get("selector"):
        container = soup.select_one(rule["selector"])
        if container and word_count(container.get_text(" ")) >= min_words:
            scoped_html = f"<html><body>{container}</body></html>"
            result.notes.append("selector")

    # 4. Main-content extraction, precision first, then recall, then readability.
    # When the page marks its article with <article> and that element holds a full text,
    # extracting inside it beats guessing from the whole page (sidebars sometimes win otherwise).
    if not rule.get("selector"):
        arts = [a for a in soup.find_all("article") if word_count(a.get_text(" ")) >= min_words]
        if len(arts) >= 1:
            biggest = max(arts, key=lambda a: word_count(a.get_text(" ")))
            md_art = _markdown_from_html(f"<html><body>{biggest}</body></html>", url, precision=True)
            if md_art:
                candidates.append(("trafilatura-article", md_art))
    md = _markdown_from_html(scoped_html, url, precision=True)
    if md:
        candidates.append(("trafilatura", md))
    md_recall = _markdown_from_html(scoped_html, url, precision=False)
    if md_recall:
        candidates.append(("trafilatura-recall", md_recall))
    md_read = _readability_markdown(scoped_html, url)
    if md_read:
        candidates.append(("readability", md_read))

    # Pick the first candidate that validates; order encodes trust.
    short_best: tuple[str, str, str, list[str]] | None = None
    for method, raw in candidates:
        cleaned, notes = scrub(raw)
        if word_count(cleaned) > config.MAX_WORDS:
            # A long report is still the article: keep its opening rather than reject it.
            cleaned = _truncate_markdown(cleaned, config.MAX_WORDS)
            notes.append("truncated")
        text = to_text(cleaned)
        err = validate(text, title, min_words, config.MAX_WORDS + 50, page_title=result.title)
        if err and "mismatch" in err and result.title and not _same_title(result.title, title):
            # The feed title and the page title differ (edited headline): judge by the page's own title.
            err = validate(text, result.title, min_words, config.MAX_WORDS, page_title=result.title)
        if err:
            result.notes.append(f"{method}: {err}")
            # Remember a short but otherwise valid body: link-blog posts and briefs are real content.
            if err.startswith("too short") and word_count(text) >= SHORT_MIN_WORDS:
                if not validate(text, title, SHORT_MIN_WORDS, config.MAX_WORDS):
                    if short_best is None or word_count(text) > word_count(short_best[2]):
                        short_best = (method, cleaned, text, notes)
            continue
        result.ok, result.method, result.markdown, result.text = True, method, cleaned, text
        result.words = word_count(text)
        result.notes.extend(notes)
        return result

    if short_best:
        method, cleaned, text, notes = short_best
        result.ok, result.method, result.markdown, result.text = True, f"{method}-short", cleaned, text
        result.words = word_count(text)
        result.notes.extend(notes)
        return result

    if not result.ok:
        result.reason = "; ".join(result.notes[-3:]) or "no candidate"
    return result


def run() -> dict:
    stats = {"processed": 0, "ok": 0, "failed": 0, "methods": {}}
    eng = db.engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(db.articles, db.sources.c.content_from_feed, db.sources.c.fulltext)
            .join(db.sources, db.articles.c.source_id == db.sources.c.id)
            .where(db.articles.c.status == "new")
            .order_by(db.articles.c.created_at.desc())
            .limit(config.MAX_EXTRACT_PER_RUN)
        ).all()

    last_para_by_domain: dict[str, str] = {}
    for row in rows:
        stats["processed"] += 1
        rule = domain_rule(row.url)
        html, err = (None, None)
        if not row.content_from_feed:
            html, err = fetch_html(row.url, agent=rule.get("agent"))
        res = extract(row.url, html, row.title, row.feed_content, row.content_from_feed)
        if err and not res.ok:
            res.reason = err

        values: dict = {"extraction_method": res.method, "extraction_ok": res.ok}
        if res.ok:
            # Sidebar fingerprint: identical last paragraph across articles of one domain.
            paras = res.markdown.split("\n\n")
            if len(paras) > 3 and last_para_by_domain.get(row.domain) == paras[-1]:
                paras = paras[:-1]
                res.markdown = "\n\n".join(paras)
                res.text = to_text(res.markdown)
            last_para_by_domain[row.domain] = paras[-1] if paras else ""
            values.update(
                content_md=res.markdown,
                content_text=res.text,
                word_count=word_count(res.text),
                image_url=row.image_url or res.image,
                author=row.author or res.author,
                status="extracted",
                show_fulltext=bool(row.fulltext) and not rule.get("fulltext") is False,
            )
            stats["ok"] += 1
            stats["methods"][res.method] = stats["methods"].get(res.method, 0) + 1
        else:
            # Keep the article as digest-only when the feed at least has a description.
            if row.description and word_count(row.description) >= 40:
                values.update(status="extracted", content_text=None, content_md=None, show_fulltext=False)
                stats["ok"] += 1
                stats["methods"]["description"] = stats["methods"].get("description", 0) + 1
            else:
                values.update(status="rejected", reject_reason=f"extract: {res.reason}"[:200])
                stats["failed"] += 1
            log.info("extract failed %s: %s", row.url[:80], res.reason)
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id == row.id).values(**values))
    return stats
