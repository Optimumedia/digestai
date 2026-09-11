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
- "model_release": null unless the article announces a new AI model or a new model version. Then: {{"name": exact model name, "lab": organisation, "kind": one of "llm", "multimodal", "image", "video", "audio", "code", "embedding", "robotics", "other", "availability": one of "api", "open_weights", "consumer", "research", "unknown", "license": license name or null, "context": context window such as "1M tokens" or null, "link": official URL mentioned or null}}.
- "funding": null unless the article reports a funding round, acquisition, or valuation for an AI company. Then: {{"company": name, "amount_usd": number in US dollars or null, "round": one of "seed", "series_a", "series_b", "series_c", "series_d_plus", "acquisition", "ipo", "debt", "other", "investors": [names], "valuation_usd": number or null}}.

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


# ----------------------------------------------------------------- budget

def _today() -> str:
    return db.utcnow().date().isoformat()


def usage_today(conn, provider: str) -> tuple[int, bool]:
    row = conn.execute(
        select(db.llm_usage.c.requests, db.llm_usage.c.exhausted)
        .where(db.llm_usage.c.day == _today(), db.llm_usage.c.provider == provider)
    ).first()
    return (row.requests, row.exhausted) if row else (0, False)


def record_usage(eng, provider: str, n: int = 1, exhausted: bool = False) -> None:
    from sqlalchemy import insert, update

    with eng.begin() as conn:
        row = conn.execute(
            select(db.llm_usage.c.id).where(db.llm_usage.c.day == _today(), db.llm_usage.c.provider == provider)
        ).first()
        if row:
            values = {"requests": db.llm_usage.c.requests + n}
            if exhausted:
                values["exhausted"] = True
            conn.execute(update(db.llm_usage).where(db.llm_usage.c.id == row.id).values(**values))
        else:
            conn.execute(insert(db.llm_usage).values(day=_today(), provider=provider, requests=n, exhausted=exhausted))


def allowance(conn, provider: str) -> int:
    """Requests this run may spend: the day's remaining budget spread over the runs still to come.

    A run that finds fewer articles leaves its share for later runs, so a busy news afternoon
    can use what a quiet morning did not, and the daily cap is never crossed.
    """
    budget = config.DAILY_BUDGET.get(provider)
    if budget is None:
        return config.MAX_ENRICH_PER_RUN
    used, exhausted = usage_today(conn, provider)
    if exhausted:
        return 0
    remaining = max(0, budget - used)
    now = db.utcnow()
    minutes_left = 24 * 60 - (now.hour * 60 + now.minute)
    runs_left = max(1, -(-minutes_left * config.RUNS_PER_DAY // (24 * 60)))  # ceil
    return max(0, min(config.MAX_ENRICH_PER_RUN, -(-remaining // runs_left)))


_gemini_dead: set[str] = set()  # models that answered 404/429 in this run


def call_gemini(prompt: str) -> dict:
    """Try the primary model, then the fallbacks; each model has its own free-tier quota."""
    last: Exception | None = None
    for model in [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]:
        if model in _gemini_dead:
            continue
        # Gemini 3 models think before answering and the thoughts count against maxOutputTokens,
        # so keep the ceiling high and the thinking short; summarising does not need deliberation.
        gen: dict = {"temperature": 0.3, "responseMimeType": "application/json", "maxOutputTokens": 8192}
        if model.startswith("gemini-3"):
            gen["thinkingConfig"] = {"thinkingLevel": "low"}
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # The key travels in a header, never in the URL, so it can never appear in an error message.
        resp = requests.post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body, timeout=90)
        if resp.status_code in (404, 429):
            _gemini_dead.add(model)
            detail = ""
            try:
                err = resp.json().get("error", {})
                quotas = [v.get("quotaId", "") for d in err.get("details", []) for v in d.get("violations", [])]
                detail = ",".join(q for q in quotas if q) or err.get("message", "")[:120]
            except Exception:  # noqa: BLE001
                pass
            last = QuotaExhausted(f"gemini {model} {resp.status_code} {detail}")
            log.warning("gemini %s unavailable (%s %s); trying next model", model, resp.status_code, detail)
            continue
        if resp.status_code >= 500:
            # Overloaded: try the next model this time, keep this one for later articles.
            last = RuntimeError(f"gemini {model} http {resp.status_code}")
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"gemini {model} http {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise RuntimeError(f"gemini {model} returned no text (finish={cand.get('finishReason')})")
        return _parse_json(text)
    raise last or QuotaExhausted("gemini: no model available")


_groq_dead: set[str] = set()
_groq_wait_until: dict[str, float] = {}  # model -> time.time() when its token window resets


def _parse_reset(value: str | None) -> float:
    """Groq reset headers look like '1.5s', '23.4s', '1m2s'; return seconds."""
    if not value:
        return 0.0
    total, num = 0.0, ""
    for ch in value:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in "hms" and num:
            total += float(num) * {"h": 3600, "m": 60, "s": 1}[ch]
            num = ""
    return total


def call_groq(prompt: str) -> dict:
    """Try the primary model, then the fallbacks; each Groq model has its own daily quota and
    a tokens-per-minute window that the response headers report."""
    last: Exception | None = None
    for model in [config.GROQ_MODEL, *config.GROQ_FALLBACK_MODELS]:
        if model in _groq_dead:
            continue
        wait = _groq_wait_until.get(model, 0.0) - time.time()
        if wait > 0:
            if wait > 45:
                continue  # let a fallback model take this one
            time.sleep(wait)
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
                "max_tokens": 3000,
            },
            timeout=90,
        )
        h = resp.headers
        if resp.status_code == 429:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:100]
            except Exception:  # noqa: BLE001
                pass
            # A per-minute limit resets in seconds; a daily one does not. Only the latter kills the model.
            reset_tokens = _parse_reset(h.get("x-ratelimit-reset-tokens"))
            reset_requests = _parse_reset(h.get("x-ratelimit-reset-requests"))
            if h.get("x-ratelimit-remaining-requests") == "0" and reset_requests > 120:
                _groq_dead.add(model)
                last = QuotaExhausted(f"groq {model} daily quota: {detail}")
                log.warning("groq %s daily quota reached; trying next model", model)
            else:
                _groq_wait_until[model] = time.time() + max(reset_tokens, reset_requests, 5.0)
                last = RuntimeError(f"groq {model} rate limited for {max(reset_tokens, reset_requests):.0f}s")
            continue
        if resp.status_code == 404:
            _groq_dead.add(model)
            last = QuotaExhausted(f"groq {model} 404")
            continue
        if resp.status_code >= 500:
            last = RuntimeError(f"groq {model} http {resp.status_code}")
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"groq {model} http {resp.status_code}: {resp.text[:160]}")
        # Pace the next call from the tokens-per-minute window so we never trip the limit.
        try:
            remaining = int(h.get("x-ratelimit-remaining-tokens", "99999"))
            if remaining < 4500:
                _groq_wait_until[model] = time.time() + _parse_reset(h.get("x-ratelimit-reset-tokens")) + 1
        except ValueError:
            pass
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_json(content)
    raise last or QuotaExhausted("groq: no model available")


def call_ollama(prompt: str) -> dict:
    resp = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": config.OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.3, "num_ctx": 4096, "num_predict": 900},
        },
        timeout=240,
    )
    resp.raise_for_status()
    return _parse_json(resp.json()["message"]["content"])


def ollama_available() -> bool:
    if not config.OLLAMA_URL:
        return False
    try:
        tags = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])
        return any(m.get("name", "").startswith(config.OLLAMA_MODEL.split(":")[0]) for m in tags)
    except Exception:  # noqa: BLE001
        return False


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
        "model_release": None,
        "funding": None,
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

    def _num(v):
        try:
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    release = result.get("model_release")
    if isinstance(release, dict) and release.get("name"):
        release = {
            "name": str(release.get("name")).strip()[:120],
            "lab": str(release.get("lab") or "").strip()[:120] or None,
            "kind": str(release.get("kind") or "other").strip().lower(),
            "availability": str(release.get("availability") or "unknown").strip().lower(),
            "license": (str(release.get("license")).strip()[:80] if release.get("license") else None),
            "context": (str(release.get("context")).strip()[:40] if release.get("context") else None),
            "link": (str(release.get("link")).strip()[:500] if release.get("link") else None),
        }
    else:
        release = None
    funding = result.get("funding")
    if isinstance(funding, dict) and funding.get("company"):
        funding = {
            "company": str(funding.get("company")).strip()[:120],
            "amount_usd": _num(funding.get("amount_usd")),
            "round": str(funding.get("round") or "other").strip().lower(),
            "investors": [str(i).strip()[:80] for i in (funding.get("investors") or []) if str(i).strip()][:10],
            "valuation_usd": _num(funding.get("valuation_usd")),
        }
    else:
        funding = None
    return {
        "model_release": release,
        "funding": funding,
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

    providers: list[tuple[str, object]] = []
    budgets: dict[str, int] = {}
    with eng.connect() as conn:
        if config.GEMINI_API_KEY:
            budgets["gemini"] = allowance(conn, "gemini")
            if budgets["gemini"] > 0:
                providers.append((f"gemini:{config.GEMINI_MODEL}", call_gemini))
        if config.GROQ_API_KEY:
            budgets["groq"] = allowance(conn, "groq")
            if budgets["groq"] > 0:
                providers.append((f"groq:{config.GROQ_MODEL}", call_groq))
    keyed = bool(config.GEMINI_API_KEY or config.GROQ_API_KEY)
    # The local model is the safety net: it takes over when the keyed providers are out of quota
    # (for the day, or mid-run), so a run never leaves the site without fresh stories.
    if ollama_available():
        providers.append((f"ollama:{config.OLLAMA_MODEL}", call_ollama))
        budgets["ollama"] = config.MAX_ENRICH_LOCAL_PER_RUN
    local_only = providers and all(p[0].startswith("ollama") for p in providers)
    stats["budget"] = budgets
    if not providers:
        if keyed:
            log.info("LLM daily budget exhausted and no local model; deferring enrichment to the next run")
            return stats
        log.warning("no LLM key configured and no local model; using heuristic enrichment")
    keyed_share = sum(v for k, v in budgets.items() if k != "ollama")
    # The local model only takes what the API providers cannot: the run's size is the larger of
    # the two shares, not their sum, so a run with a healthy Groq budget finishes in ~10 minutes.
    limit = max(keyed_share, budgets.get("ollama", 0)) if providers else config.MAX_ENRICH_PER_RUN
    input_words = config.LOCAL_INPUT_WORDS if local_only else config.LLM_INPUT_WORDS
    spent: dict[str, int] = {}

    with eng.connect() as conn:
        rows = conn.execute(
            select(db.articles, db.sources.c.category_hint, db.sources.c.name.label("source_name"), db.sources.c.weight)
            .join(db.sources, db.articles.c.source_id == db.sources.c.id)
            .where(db.articles.c.status == "gated")
            # The scarce, best model takes the first rows, so the labs' own announcements and
            # the strongest press come first; the local model takes the rest, freshest first.
            .order_by(db.sources.c.weight.desc(), db.articles.c.published_at.desc())
            .limit(limit)
        ).all()

    for row in rows:
        text = row.content_text or row.description or ""
        prompt = PROMPT.format(
            categories=", ".join(f'"{k}" ({v})' for k, v in config.CATEGORIES.items()),
            title=row.title,
            source=row.source_name,
            published=row.published_at.isoformat() if row.published_at else "unknown",
            text=_truncate(text, input_words),
        )
        result, model_used = None, "heuristic"
        for name, fn in list(providers):
            provider = name.split(":")[0]
            if provider in budgets and spent.get(provider, 0) >= budgets[provider]:
                continue  # this provider's share for the run is spent; try the next one
            if provider in ("ollama", "groq"):
                # Shorter input for the CPU model (speed) and for Groq (tokens-per-minute window).
                words = config.LOCAL_INPUT_WORDS if provider == "ollama" else config.GROQ_INPUT_WORDS
                prompt = PROMPT.format(
                    categories=", ".join(f'"{k}" ({v})' for k, v in config.CATEGORIES.items()),
                    title=row.title, source=row.source_name,
                    published=row.published_at.isoformat() if row.published_at else "unknown",
                    text=_truncate(text, words),
                )
            try:
                result = fn(prompt)
                model_used = name
                spent[provider] = spent.get(provider, 0) + 1
                if provider in budgets and provider != "ollama":
                    record_usage(eng, provider, 1)
                break
            except QuotaExhausted as exc:
                log.warning("%s quota exhausted (%s); switching provider", name, exc)
                if provider in budgets:
                    record_usage(eng, provider, 0, exhausted=True)
                providers = [p for p in providers if p[0] != name]
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("%s failed on #%s: %s", name, row.id, exc)
        if result is None:
            remaining = [p for p in providers if spent.get(p[0].split(":")[0], 0) < budgets.get(p[0].split(":")[0], 10**9)]
            if remaining:
                # A transient error: leave the row for the next run rather than degrade it.
                continue
            if keyed or budgets.get("ollama"):
                break  # every provider's share for this run is spent; the rest waits for the next run
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
                model_release=clean["model_release"],
                funding=clean["funding"],
                enrich_model=model_used,
                status="enriched",
            ))
        if model_used.startswith("gemini"):
            time.sleep(4.2)  # 15 requests per minute on the free tier
        # Groq paces itself from its rate-limit headers inside call_groq.
    return stats
