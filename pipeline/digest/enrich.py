"""Step 4: one LLM call per article. Gemini Flash (free tier) first, Groq as fallback,
a heuristic path when neither key is configured so the pipeline always completes."""
from __future__ import annotations

import json
import logging
import re
import time

import requests
from sqlalchemy import select, update

from . import config, db
from .textutil import first_sentences, word_count

log = logging.getLogger("digest.enrich")

PROMPT = """You are the news editor of Digest AI, a site that covers artificial intelligence.
Read the article below and return ONLY a JSON object with these fields:

- "headline": a clear, specific headline, max 90 characters, no source name, no clickbait.
- "summary_md": an original 150-300 word digest in 2-3 short paragraphs, plain Markdown, written in your own words. State what happened, who is involved, the key numbers, and context a busy reader needs. Do not copy sentences from the article. Do not start with "The article".
- "key_points": exactly 3 bullet strings, each max 25 words, the most important concrete facts.
- "why_it_matters": max 60 words on the significance for the AI industry or the public.
- "category": one of {categories}.
- "entities": {{"companies": [...], "models": [...], "people": [...]}} with proper names actually mentioned, max 6 each.
- "content_type": one of "news", "analysis", "research", "tutorial", "opinion", "press_release", "listicle", "product".
- "importance": integer 1-10. 10 = a frontier model release, major regulation, or a deal above $1B. 5 = routine industry news. 2 = a minor product update or a tutorial.
- "is_ai_news": true if the article is substantially about artificial intelligence, machine learning, robotics, or AI hardware; false otherwise.

Article title: {title}
Source: {source}
Published: {published}

Article text:
\"\"\"
{text}
\"\"\"
"""


def _truncate(text: str, words: int) -> str:
    parts = text.split()
    return " ".join(parts[:words])


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json object in response")
    return json.loads(raw[start : end + 1])


class QuotaExhausted(Exception):
    pass


def call_gemini(prompt: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json", "maxOutputTokens": 2048},
    }
    resp = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=60)
    if resp.status_code == 429:
        raise QuotaExhausted("gemini 429")
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_json(text)


def call_groq(prompt: str) -> dict:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": config.GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
            "max_tokens": 2048,
        },
        timeout=60,
    )
    if resp.status_code == 429:
        raise QuotaExhausted("groq 429")
    resp.raise_for_status()
    return _parse_json(resp.json()["choices"][0]["message"]["content"])


def heuristic(row, category_hint: str | None) -> dict:
    text = row.content_text or row.description or ""
    sents = first_sentences(text, 6)
    summary = " ".join(sents[:4]) if sents else (row.description or row.title)
    return {
        "headline": row.title,
        "summary_md": summary,
        "key_points": sents[:3] or [row.title],
        "why_it_matters": "",
        "category": category_hint or "models",
        "entities": {"companies": [], "models": [], "people": []},
        "content_type": "news",
        "importance": 5,
        "is_ai_news": True,
    }


def _clean(result: dict, row, category_hint: str | None) -> dict:
    cats = set(config.CATEGORIES)
    category = str(result.get("category", "")).strip().lower()
    if category not in cats:
        category = category_hint if category_hint in cats else "models"
    headline = str(result.get("headline") or row.title).strip()[:160]
    key_points = [str(k).strip() for k in (result.get("key_points") or []) if str(k).strip()][:3]
    entities = result.get("entities") or {}
    entities = {
        k: [str(v).strip() for v in (entities.get(k) or []) if str(v).strip()][:6]
        for k in ("companies", "models", "people")
    }
    try:
        importance = max(1, min(10, int(result.get("importance", 5))))
    except (TypeError, ValueError):
        importance = 5
    ctype = str(result.get("content_type", "news")).strip().lower()
    if ctype not in {"news", "analysis", "research", "tutorial", "opinion", "press_release", "listicle", "product"}:
        ctype = "news"
    return {
        "headline": headline,
        "summary_md": str(result.get("summary_md") or "").strip(),
        "key_points": key_points,
        "why_it_matters": str(result.get("why_it_matters") or "").strip()[:600],
        "category": category,
        "entities": entities,
        "content_type": ctype,
        "importance": importance,
        "is_ai_news": bool(result.get("is_ai_news", True)),
    }


def run() -> dict:
    stats = {"enriched": 0, "rejected": 0, "errors": 0, "model": None}
    eng = db.engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(db.articles, db.sources.c.category_hint, db.sources.c.name.label("source_name"), db.sources.c.weight)
            .join(db.sources, db.articles.c.source_id == db.sources.c.id)
            .where(db.articles.c.status == "gated")
            # Freshest first: a 30-minute news cadence matters more than source prestige.
            .order_by(db.articles.c.published_at.desc(), db.sources.c.weight.desc())
            .limit(config.MAX_ENRICH_PER_RUN)
        ).all()

    providers: list[tuple[str, object]] = []
    if config.GEMINI_API_KEY:
        providers.append((f"gemini:{config.GEMINI_MODEL}", call_gemini))
    if config.GROQ_API_KEY:
        providers.append((f"groq:{config.GROQ_MODEL}", call_groq))
    if not providers:
        log.warning("no LLM key configured; using heuristic enrichment")

    for row in rows:
        text = row.content_text or row.description or ""
        prompt = PROMPT.format(
            categories=", ".join(f'"{k}" ({v})' for k, v in config.CATEGORIES.items()),
            title=row.title,
            source=row.source_name,
            published=row.published_at.isoformat() if row.published_at else "unknown",
            text=_truncate(text, config.LLM_INPUT_WORDS),
        )
        result, model_used = None, "heuristic"
        for name, fn in list(providers):
            try:
                result = fn(prompt)
                model_used = name
                break
            except QuotaExhausted as exc:
                log.warning("%s quota exhausted (%s); switching provider", name, exc)
                providers = [p for p in providers if p[0] != name]
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("%s failed on #%s: %s", name, row.id, exc)
        if result is None:
            if providers:
                # A transient error: leave the row for the next run rather than degrade it.
                continue
            result = heuristic(row, row.category_hint)
        stats["model"] = model_used
        clean = _clean(result, row, row.category_hint)
        with eng.begin() as conn:
            if not clean["is_ai_news"]:
                stats["rejected"] += 1
                conn.execute(update(db.articles).where(db.articles.c.id == row.id)
                             .values(status="rejected", reject_reason="llm: not ai news", enrich_model=model_used))
                continue
            stats["enriched"] += 1
            conn.execute(update(db.articles).where(db.articles.c.id == row.id).values(
                headline=clean["headline"],
                summary_md=clean["summary_md"],
                key_points=clean["key_points"],
                why_it_matters=clean["why_it_matters"],
                category=clean["category"],
                entities=clean["entities"],
                content_type=clean["content_type"],
                importance=clean["importance"],
                enrich_model=model_used,
                status="enriched",
            ))
        if model_used.startswith("gemini"):
            time.sleep(4.2)  # 15 requests per minute on the free tier
        elif model_used.startswith("groq"):
            time.sleep(2.1)
    return stats
