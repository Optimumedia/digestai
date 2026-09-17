"""Step 4: one LLM call per article. Gemini Flash (free tier) first, Groq as fallback,
a heuristic path when neither key is configured so the pipeline always completes."""
from __future__ import annotations

import json
import logging
import re
import time

import requests
from sqlalchemy import select, update

from . import config, db, work
from .textutil import first_sentences, word_count

log = logging.getLogger("digest.enrich")

PROMPT = """You are the news editor of Digest AI, a site that covers artificial intelligence.
Read the article below and return ONLY a JSON object with these fields:

- "headline": a clear, specific headline, max 90 characters, no source name, no clickbait. Keep the source's hedging: if the article says may, could, might, reportedly, allegedly or according to, or asks a question, the headline must keep that hedge (for example "Anthropic may be monitoring AI critics, report says"). Never state as fact what the source frames as a question, a possibility, an allegation or an opinion. If the piece is an opinion column or an essay, start the headline with "Opinion:".
- "summary_md": an original 150-300 word digest in 2-3 short paragraphs, plain Markdown, written in your own words. State what happened, who is involved, the key numbers, and context a busy reader needs. Keep the article's hedging and attribution (who claims what, and what is unconfirmed), and say when it is an opinion piece. Do not copy sentences from the article. Do not start with "The article".
- "key_points": exactly 3 bullet strings, each max 25 words, the most important concrete facts.
- "why_it_matters": max 60 words on the significance for the AI industry or the public.
- "category": one of {categories}. Use "marketing" for practical AI that marketers and small businesses can use: AI features and tools for content, ads, SEO and search visibility, email, social media, sales, customer service, e-commerce, bookkeeping and everyday automation, including how-to guides and playbooks. Prefer "marketing" over "agents" or "enterprise" when the reader who can act on it is a marketer or a small business owner.
- "entities": {{"companies": [...], "models": [...], "people": [...]}} with proper names actually mentioned, max 6 each.
- "content_type": one of "news", "analysis", "research", "tutorial", "opinion", "press_release", "listicle", "product".
- "importance": integer 1-10. 10 = a frontier model release, major regulation, or a deal above $1B. 5 = routine industry news. 2 = a minor product update or a tutorial. For "marketing", rate usefulness to a marketer or small business: a new AI capability in a widely used tool (Google Ads, Meta, Shopify, HubSpot, Canva, ChatGPT, Gemini) or a change that affects search traffic = 6-7; a concrete, current how-to = 4-5; generic tips = 2-3.
- "is_ai_news": true if the article is substantially about artificial intelligence, machine learning, robotics, or AI hardware; false otherwise.
- "model_release": null unless the article announces a new AI model or a new model version. Then: {{"name": the model's full official name including version number as the lab writes it (e.g. "GPT-6 Astra", "WeatherNext 3", not "GPT-6" or "WeatherNext"), "lab": organisation, "kind": one of "llm", "multimodal", "image", "video", "audio", "code", "embedding", "robotics", "other", "availability": one of "api", "open_weights", "consumer", "research", "unknown", "license": license name or null, "context": context window such as "1M tokens" or null, "link": official URL mentioned or null}}.
- "funding": null unless the article reports a funding round, acquisition, or valuation for an AI company. Then: {{"company": name, "amount_usd": approximate number in US dollars (convert euros, pounds or other currencies at current rates; e.g. €3B is about 3300000000) or null, "round": one of "seed", "series_a", "series_b", "series_c", "series_d_plus", "acquisition", "ipo", "debt", "other", "investors": [names], "valuation_usd": number or null}}.
- "work_card": null unless a marketer, a small-business owner or a small team can act on this today: a tool they can use, a feature or price change in a tool they already use, a how-to, or a policy change that affects their work. Industry news, funding rounds, research papers, model benchmarks and opinion pieces get null, however interesting. Then: {{"fits": true, "tool": the product's name, "maker": the company behind it, "what_it_does": one plain sentence, max 25 words, no marketing words, "who_for": one or more of "marketer", "sales", "founder", "support", "ops", "ecommerce", "use_for": exactly 3 concrete things to use it for, max 12 words each, "cost": one of "free", "free tier", "paid from $X" with the real figure, or "included in a tool you already have", "effort": one of "minutes", "an afternoon", "needs a developer", "watch_out": one honest sentence on the catch - a limit, a risk, a cost, a country restriction, or who it is not for; never "none", "link": the official URL if the article names one, else null}}.

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


class ProviderPaused(Exception):
    """A usage limit that resets within hours: skip the provider for this run, not the whole day."""


# ----------------------------------------------------------------- time budget
# The step's deadline (time.monotonic()); provider calls size their timeouts and rate-limit
# waits to it, so one slow call cannot carry the step far past ENRICH_TIME_BUDGET_SECONDS.
_deadline: float | None = None
GROQ_MAX_WAIT_SECONDS = 20.0  # longer rate-limit windows go to a fallback model instead


def _time_left() -> float:
    return float("inf") if _deadline is None else _deadline - time.monotonic()


def _request_timeout(cap: float) -> float:
    return max(5.0, min(cap, _time_left()))


def should_start_article(elapsed: float, durations: list[float], budget: float, first_estimate: float) -> bool:
    """Start another article only if it is expected to finish inside the budget. The estimate is
    the running average, raised toward the slowest article so far (rate-limit stalls come in runs)."""
    if durations:
        estimate = max(sum(durations) / len(durations), 0.5 * max(durations))
    else:
        estimate = first_estimate
    return elapsed + estimate <= budget


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
        resp = requests.post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body, timeout=_request_timeout(90))
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
            # Sleeping through a long window, once per model, used to cost 2-3 minutes per article.
            if wait > min(GROQ_MAX_WAIT_SECONDS, _time_left() - 30.0):
                # Rate limited for a while: let a fallback take this one. This is a retry
                # condition, never a daily-quota one, so the day must not be marked exhausted.
                last = RuntimeError(f"groq {model} rate limited for {wait:.0f}s")
                continue
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
            timeout=_request_timeout(90),
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
        if resp.status_code == 400 and "json" in resp.text.lower():
            # This model could not produce valid JSON for this article; another model usually can.
            last = RuntimeError(f"groq {model} could not produce JSON")
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


_cloud_dead: set[str] = set()  # Ollama Cloud models that need a paid plan (402) or do not exist


def call_ollama_cloud(prompt: str) -> dict:
    """Ollama Cloud's free models, primary first. 402/403/404: that model is not available on the free
    plan, try the next. 429: the free usage limit, which resets within hours, so the cloud sits out
    the rest of this run and is tried again next run."""
    last: Exception | None = None
    for model in [config.OLLAMA_CLOUD_MODEL, *config.OLLAMA_CLOUD_FALLBACK_MODELS]:
        if model in _cloud_dead:
            continue
        resp = requests.post(
            f"{config.OLLAMA_CLOUD_URL}/api/chat",
            headers={"Authorization": f"Bearer {config.OLLAMA_API_KEY}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "stream": False,
                "think": False,
                "options": {"temperature": 0.3},
            },
            timeout=_request_timeout(120),
        )
        if resp.status_code in (401, 402, 403, 404):
            _cloud_dead.add(model)
            last = QuotaExhausted(f"ollama cloud {model} http {resp.status_code}: {resp.text[:100]}")
            continue
        if resp.status_code == 429:
            raise ProviderPaused(f"ollama cloud usage limit: {resp.text[:120]}")
        if resp.status_code >= 500:
            last = RuntimeError(f"ollama cloud {model} http {resp.status_code}")
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"ollama cloud {model} http {resp.status_code}: {resp.text[:160]}")
        try:
            return _parse_json(resp.json()["message"]["content"])
        except Exception as exc:  # noqa: BLE001 - another model usually manages valid JSON
            last = RuntimeError(f"ollama cloud {model} gave no usable JSON: {str(exc)[:80]}")
    raise last or QuotaExhausted("ollama cloud: no model available")


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
        timeout=_request_timeout(240),
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
        "work_card": None,
    }


# Words that mark a source's claim as unconfirmed: a possibility, an allegation, a rumour. Kept
# narrow on purpose: "Anthropic says Claude blocked..." is a company describing its own action, and
# flagging every "says" would bury the real cases on the dashboard.
SOURCE_HEDGE = re.compile(
    r"\b(?:may|might|could|reportedly|allegedly|alleged|according to|claims?|claimed|suggests?|"
    r"appears? to|seems? to|possibl[ey]|rumou?r(?:s|ed)?|sources? (?:say|said|tell)|is said to|are said to|"
    r"report says|reports say|in talks|considering|opinion)\b", re.I)
# What counts as our headline keeping a hedge or an attribution: any of the above, or a named source.
HEADLINE_HEDGE = re.compile(
    SOURCE_HEDGE.pattern[:-3] + r"|says?|said|tells?|told|report(?:s|ed)?|likely|would|expected to|plans? to|"
    r"accus(?:es|ed)|warns?|argues?|opinion:?)\b", re.I)
QUESTION = re.compile(r"\?")


def title_hedges(title: str | None) -> bool:
    """The source's own title frames its claim as a question, a possibility or an allegation."""
    return bool(title) and bool(QUESTION.search(title) or SOURCE_HEDGE.search(title))


def headline_hedged(title: str | None, headline: str | None) -> bool:
    """True when the source title hedges (a question, may/could/reportedly/allegedly/according to)
    and our headline keeps neither a hedge nor an attribution, stating the claim as fact. Rewriting is
    not something a cheap check can do, so the flag is reported (export.py, quality.py), not fixed."""
    if not title or not headline or title.strip() == headline.strip():
        return False
    return title_hedges(title) and not (QUESTION.search(headline) or HEADLINE_HEDGE.search(headline))


def _as_list(value) -> list:
    """Models sometimes return a single string, a number or null where a list belongs."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _clean(result: dict, row, category_hint: str | None) -> dict:
    if not isinstance(result, dict):
        result = {}
    cats = set(config.CATEGORIES)
    category = str(result.get("category", "")).strip().lower()
    if category not in cats:
        category = category_hint if category_hint in cats else "models"
    headline = str(result.get("headline") or row.title).strip()[:160]
    key_points = [str(k).strip() for k in _as_list(result.get("key_points")) if str(k).strip()][:3]
    entities = result.get("entities") if isinstance(result.get("entities"), dict) else {}
    entities = {
        k: [str(v).strip() for v in _as_list(entities.get(k)) if str(v).strip()][:6]
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
            "investors": [str(i).strip()[:80] for i in _as_list(funding.get("investors")) if str(i).strip()][:10],
            "valuation_usd": _num(funding.get("valuation_usd")),
        }
    else:
        funding = None
    return {
        "model_release": release,
        "funding": funding,
        # AI at Work (/work): validated and clamped in work.py, which drops a card that names no
        # tool or carries no honest caveat, so no half-card can reach a page.
        "work_card": work.clean_card(result.get("work_card")),
        "headline": headline,
        "summary_md": str(result.get("summary_md") or "").strip(),
        "key_points": key_points,
        "why_it_matters": str(result.get("why_it_matters") or "").strip()[:600],
        "category": category,
        "entities": entities,
        "content_type": ctype,
        "importance": importance,
        "is_ai_news": bool(result.get("is_ai_news", True)),
        # The source hedged and the model did not: counted in the run's stats and flagged again at
        # export time from the stored title and headline (no column needed), where quality.py lists it.
        "hedged": headline_hedged(getattr(row, "title", None), headline),
    }


def run() -> dict:
    global _deadline
    _deadline = None
    stats = {"enriched": 0, "rejected": 0, "errors": 0, "model": None}
    eng = db.engine()

    providers: list[tuple[str, object]] = []
    budgets: dict[str, int] = {}
    with eng.connect() as conn:
        if config.GEMINI_API_KEY:
            budgets["gemini"] = allowance(conn, "gemini")
            if budgets["gemini"] > 0:
                providers.append((f"gemini:{config.GEMINI_MODEL}", call_gemini))
        if config.OLLAMA_API_KEY:
            budgets["cloud"] = allowance(conn, "cloud")
            if budgets["cloud"] > 0:
                providers.append((f"cloud:{config.OLLAMA_CLOUD_MODEL}", call_ollama_cloud))
        if config.GROQ_API_KEY:
            budgets["groq"] = allowance(conn, "groq")
            if budgets["groq"] > 0:
                providers.append((f"groq:{config.GROQ_MODEL}", call_groq))
    keyed = bool(config.GEMINI_API_KEY or config.GROQ_API_KEY or config.OLLAMA_API_KEY)
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
            # Only the columns used here: the article's markdown copy, feed text and embedding are not.
            select(db.articles.c.id, db.articles.c.title, db.articles.c.description, db.articles.c.content_text,
                   db.articles.c.published_at, db.sources.c.category_hint, db.sources.c.name.label("source_name"), db.sources.c.weight)
            .join(db.sources, db.articles.c.source_id == db.sources.c.id)
            .where(db.articles.c.status == "gated")
            # The scarce, best model takes the first rows, so the labs' own announcements and
            # the strongest press come first; the local model takes the rest, freshest first.
            .order_by(db.sources.c.weight.desc(), db.articles.c.published_at.desc())
            .limit(limit)
        ).all()

    budget = float(config.ENRICH_TIME_BUDGET_SECONDS)
    started = time.monotonic()
    _deadline = started + budget
    durations: list[float] = []
    last_start: float | None = None
    for row in rows:
        now_m = time.monotonic()
        if last_start is not None:
            durations.append(now_m - last_start)
        if not should_start_article(now_m - started, durations, budget, config.ENRICH_FIRST_ARTICLE_ESTIMATE_SECONDS):
            # Leave the rest for the next run: slow or rate-limited providers must not push the
            # whole run past the workflow's time limit, because a cancelled run publishes nothing.
            # Checked before an article starts, with its expected duration, so the budget holds.
            stats["stopped_for_time"] = True
            break
        last_start = now_m
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
            except ProviderPaused as exc:
                log.warning("%s paused for this run (%s); switching provider", name, exc)
                providers = [p for p in providers if p[0] != name]
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
        try:
            clean = _clean(result, row, row.category_hint)
        except Exception as exc:  # noqa: BLE001 - one malformed answer must not stop the batch
            stats["errors"] += 1
            log.warning("could not use the answer for #%s from %s: %s", row.id, model_used, exc)
            continue  # the article stays pending and is retried next run
        with eng.begin() as conn:
            if not clean["is_ai_news"]:
                stats["rejected"] += 1
                conn.execute(update(db.articles).where(db.articles.c.id == row.id)
                             .values(status="rejected", reject_reason="llm: not ai news", enrich_model=model_used))
                continue
            stats["enriched"] += 1
            if clean["work_card"]:
                stats["work_cards"] = stats.get("work_cards", 0) + 1
            if clean["hedged"]:
                stats["hedged"] = stats.get("hedged", 0) + 1
                log.info("headline drops the source's hedge on #%s: %r -> %r", row.id, (row.title or "")[:80], clean["headline"][:80])
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
                work_card=clean["work_card"],
                enrich_model=model_used,
                status="enriched",
            ))
        if model_used.startswith("gemini"):
            time.sleep(4.2)  # 15 requests per minute on the free tier
        # Groq paces itself from its rate-limit headers inside call_groq.
    _deadline = None
    stats["seconds_used"] = round(time.monotonic() - started, 1)
    return stats
