"""Step 3: quality gate. Cheap checks that run before any LLM call."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from langdetect import DetectorFactory, LangDetectException, detect
from sqlalchemy import func, or_, select, update

from . import cache, config, db
from .textutil import hamming, title_year, word_count

DetectorFactory.seed = 0
log = logging.getLogger("digest.gate")

# (pattern, weight). Title hits count triple.
RELEVANCE = [
    (r"\b(ai|a\.i\.)\b", 2), (r"artificial intelligence", 3), (r"machine learning", 3), (r"\bml\b", 1),
    (r"\b(llm|llms)\b", 3), (r"large language model", 3), (r"language model", 2), (r"foundation model", 3),
    (r"\bgpt-?\d", 3), (r"\bchatgpt\b", 3), (r"\bopenai\b", 3), (r"\banthropic\b", 3), (r"\bclaude\b", 2),
    (r"\bgemini\b", 2), (r"\bdeepmind\b", 3), (r"\bllama\b", 2), (r"\bmistral\b", 2), (r"\bcopilot\b", 2),
    (r"\bnvidia\b", 2), (r"\bgpu(s)?\b", 1), (r"\btpu(s)?\b", 2), (r"neural network", 3), (r"deep learning", 3),
    (r"\btransformer(s)?\b", 2), (r"generative", 2), (r"diffusion model", 3), (r"\bagent(s|ic)?\b", 1),
    (r"\bchatbot(s)?\b", 2), (r"\brobot(s|ics)?\b", 1), (r"humanoid", 2), (r"autonomous", 1),
    (r"\binference\b", 2), (r"fine-?tun", 2), (r"\brag\b", 1), (r"embedding(s)?", 1), (r"hallucinat", 2),
    (r"\balignment\b", 1), (r"\bai act\b", 3), (r"superintelligen", 3), (r"\bagi\b", 3), (r"hugging face", 3),
    (r"\bxai\b", 2), (r"\bgrok\b", 2), (r"\bperplexity\b", 2), (r"\bcursor\b", 1), (r"data cent(er|re)", 1),
    (r"\bmultimodal\b", 3), (r"text-to-(image|video|speech)", 3), (r"\bsora\b", 2), (r"\bmidjourney\b", 3),
]
RELEVANCE_RX = [(re.compile(p, re.IGNORECASE), w) for p, w in RELEVANCE]

TITLE_BLOCK = re.compile(
    r"\b(porn|xxx|sex|nude|casino|betting|slots|crypto casino|coupon|promo code|discount code|"
    r"webinar|eventbrite|register now|hiring|job opening|we're hiring|vacanc|giveaway|sweepstake|"
    r"horoscope|lottery|essay writing service|buy followers)\b",
    re.IGNORECASE,
)
DOMAIN_BLOCK = {
    "eventbrite.com", "eventbrite.co.uk", "meetup.com", "indeed.com", "glassdoor.com", "linkedin.com",
    "prnewswire.com", "globenewswire.com", "businesswire.com", "einpresswire.com", "openpr.com",
    "medium.com", "dev.to", "quora.com", "pinterest.com", "slideshare.net", "scribd.com",
}
PRESS_RELEASE = re.compile(r"\b(press release|pr newswire|globe ?newswire|business wire|for immediate release)\b", re.IGNORECASE)


def relevance_score(title: str, text: str) -> float:
    score = 0.0
    for rx, w in RELEVANCE_RX:
        score += 3 * w * len(rx.findall(title or ""))
    body = (text or "")[:6000]
    words = max(1, word_count(body))
    body_score = sum(w * len(rx.findall(body)) for rx, w in RELEVANCE_RX)
    score += body_score * 200 / words  # per 200 words
    return score


def detect_lang(text: str) -> str:
    sample = (text or "")[:1500]
    if word_count(sample) < 8:
        return "unknown"
    try:
        return detect(sample)
    except LangDetectException:
        return "unknown"


def check(row, recent_hashes: list[tuple[int, int]]) -> str | None:
    """Return a reject reason or None."""
    text = row.content_text or row.description or ""
    if row.domain in DOMAIN_BLOCK:
        return f"blocked domain {row.domain}"
    if TITLE_BLOCK.search(row.title or ""):
        return "blocked title pattern"
    lang = detect_lang(f"{row.title}. {text}")
    if lang not in ("en", "unknown"):
        return f"language {lang}"
    now = db.utcnow()
    pub = db.as_utc(row.published_at)
    if pub and pub > now + timedelta(hours=2):
        return "published in the future"
    if pub and pub < now - timedelta(days=config.MAX_ARTICLE_AGE_DAYS):
        return "too old"
    # "GPT2 will not be released (2019)": a community re-post of an old piece. Its submission
    # time is recent, so only the title tells us the story is years old.
    year = title_year(getattr(row, "raw_title", None) or row.title or "")
    if year and year < now.year:
        return f"too old (title says {year})"
    # Borderline pieces (score 4-6) go through; the LLM's is_ai_news check is the second gate.
    score = relevance_score(row.title or "", text)
    if score < 4:
        return f"not about AI (score {score:.1f})"
    if PRESS_RELEASE.search(text[:600]) and score < 12:
        return "press release"
    if row.simhash is not None:
        mine = db.from_signed64(row.simhash)
        for other_id, other in recent_hashes:
            # Measured on real titles: syndicated copies sit at 0-8, distinct titles at 13+.
            if other_id != row.id and hamming(mine, other) <= 10:
                return f"duplicate title of #{other_id}"
    return None


OLD_TITLE_PATTERNS = ["%(19__)%", "%(20__)%", "%[19__]%", "%[20__]%"]


def remove_old_published(eng, days: int = 30) -> int:
    """One-off repair, cheap on every run: articles published before this check existed whose
    title marks them as years old (the 2019 GPT-2 post on the front page) are rejected, so the
    export drops them and any story left without articles."""
    now = db.utcnow()
    with eng.begin() as conn:
        rows = conn.execute(
            select(db.articles.c.id, db.articles.c.raw_title, db.articles.c.title)
            .where(db.articles.c.status == "published", db.articles.c.created_at >= now - timedelta(days=days),
                   or_(*[db.articles.c.raw_title.like(p) for p in OLD_TITLE_PATTERNS],
                       *[db.articles.c.title.like(p) for p in OLD_TITLE_PATTERNS]))
        ).all()
        removed = 0
        for r in rows:
            year = title_year(r.raw_title or r.title or "")
            if year and year < now.year:
                conn.execute(update(db.articles).where(db.articles.c.id == r.id)
                             .values(status="rejected", reject_reason=f"gate: too old (title says {year})"))
                removed += 1
    return removed


def run() -> dict:
    stats = {"checked": 0, "passed": 0, "rejected": 0, "reasons": {}}
    eng = db.engine()
    old = remove_old_published(eng)
    if old:
        stats["old_published_removed"] = old
    since = db.utcnow() - timedelta(days=7)
    with eng.connect() as conn:
        a = db.articles.c
        # The checks look at no more than the first 6,000 characters of the text (relevance_score),
        # so no more than that is read.
        rows = conn.execute(
            select(a.id, a.url, a.domain, a.title, a.raw_title, a.published_at, a.simhash, a.description,
                   func.substr(a.content_text, 1, 6000).label("content_text"))
            .where(a.status == "extracted")
        ).all()
        recent = sorted((r for r in cache.articles(conn).values()
                         if r.status in ("enriched", "published") and r.simhash is not None and db.as_utc(r.created_at) >= since),
                        key=lambda r: r.id)
    recent_hashes = [(r.id, db.from_signed64(r.simhash)) for r in recent]
    for row in rows:
        stats["checked"] += 1
        reason = check(row, recent_hashes)
        with eng.begin() as conn:
            if reason:
                stats["rejected"] += 1
                key = reason.split(" (")[0].split(" #")[0]
                stats["reasons"][key] = stats["reasons"].get(key, 0) + 1
                conn.execute(update(db.articles).where(db.articles.c.id == row.id)
                             .values(status="rejected", reject_reason=f"gate: {reason}"[:200], lang=detect_lang(row.title or "")))
            else:
                stats["passed"] += 1
                conn.execute(update(db.articles).where(db.articles.c.id == row.id).values(status="gated", lang="en"))
                if row.simhash is not None:
                    recent_hashes.append((row.id, db.from_signed64(row.simhash)))
    return stats
