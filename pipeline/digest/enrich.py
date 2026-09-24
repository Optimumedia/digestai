"""Step 4: one LLM call per article. Gemini Flash (free tier) first, then Ollama Cloud, Groq,
Cloudflare Workers AI (daily neurons), OpenRouter (free models, daily requests) and Mistral
(Ministral 8B, capped by monthly spend), then a local model; a heuristic path when no key is
configured so the pipeline always completes."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import func, select, update

from . import checks, config, db, work
from .textutil import first_sentences, keywords

log = logging.getLogger("digest.enrich")

SCHEMA = """You are the news editor of Digest AI, a site that covers artificial intelligence.
Read the article below and return ONLY a JSON object with these fields:

- "headline": a plain, specific news headline under 90 characters: subject, verb, object - name the company or person and the thing (product, model, deal, ruling), with the key figure if there is one, e.g. "Google adds Gemini 4 to Chrome for US users". No source name, no hype words (revolutionizes, game-changer, unleashes), no teasers ("here's why", "everything you need to know"), no exclamation marks, no words in capitals, at most one colon, and a question mark only when the article itself asks. Keep the source's hedging: if the article says may, could, might, reportedly, allegedly or according to, or asks a question, the headline must keep it ("Anthropic may be monitoring AI critics, report says"). Never state as fact what the source frames as a question, a possibility or an allegation. An opinion column or essay starts with "Opinion:".
- "summary_md": an original 150-300 word digest in 2-3 short paragraphs, plain Markdown, written in your own words. State what happened, who is involved, the key numbers, and context a busy reader needs. Keep the article's hedging and attribution (who claims what, and what is unconfirmed), and say when it is an opinion piece. Do not copy sentences, and do not start with "The article".
- "key_points": exactly 3 bullet strings, each max 25 words, the most important concrete facts.
- "why_it_matters": max 60 words on the significance for the AI industry or the public.
- "category": one of {categories}. Use "marketing" for practical AI that marketers and small businesses can use: tools for content, ads, SEO and search visibility, email, social media, sales, customer service, e-commerce, bookkeeping, everyday automation and how-tos. Prefer "marketing" over "agents" or "enterprise" when a marketer or small business owner can act on it.
- "entities": {{"companies": [...], "models": [...], "people": [...]}} with proper names actually mentioned, max 6 each.
- "content_type": one of "news", "analysis", "research", "tutorial", "opinion", "press_release", "listicle", "product".
- "importance": integer 1-10. 10 = a frontier model release, major regulation, or a deal above $1B. 5 = routine industry news. 2 = a minor product update or a tutorial.{ladder} For "marketing", rate usefulness to a marketer or small business: a new AI capability in a widely used tool (Google Ads, Meta, Shopify, HubSpot, Canva, ChatGPT, Gemini) or a change that affects search traffic = 6-7; a concrete, current how-to = 4-5; generic tips = 2-3.
- "is_ai_news": true if the article is substantially about AI, machine learning, robotics or AI hardware, else false.
- "model_release": null unless the article announces a new AI model or a new model version. Then: {{"name": the model's full official name with its version number as the lab writes it (e.g. "GPT-6 Astra", not "GPT-6"), "lab": organisation, "kind": one of "llm", "multimodal", "image", "video", "audio", "code", "embedding", "robotics", "other", "availability": one of "api", "open_weights", "consumer", "research", "unknown", "license": license name or null, "context": context window such as "1M tokens" or null, "link": official URL mentioned or null}}.
- "funding": null unless the article reports a funding round, acquisition, or valuation for an AI company. Then: {{"company": name, "amount_usd": approximate number in US dollars (convert other currencies at current rates; e.g. €3B is about 3300000000) or null, "round": one of "seed", "series_a", "series_b", "series_c", "series_d_plus", "acquisition", "ipo", "debt", "other", "investors": [names], "valuation_usd": number or null}}.
- "work_card": null unless a marketer or a small business can act on it today: a tool, a feature or price change in a tool they use, a how-to, or a policy change affecting their work. Industry news, funding, research, benchmarks, opinion, developer or cloud tools and courses get null. Never a category ("AI SEO tools", "AI agents"): name one product. Then: {{"fits": true, "tool": the product's name, "maker": its company, "headline": <HEADLINE>, "you_get": <YOU_GET>, "what_it_does": <WHAT>, "who_for": one or more of "marketer", "sales", "founder", "support", "ops", "ecommerce", "use_for": <USES>, "cost": "free", "free tier", "paid from $X" with the real figure, "included in a tool you already have" or "not stated", "price": {{"plan": the plan's name or "", "amount": the number only, "currency": "USD"/"EUR"/"GBP", "period": "month"/"year"/"one-off"/"usage", "free_limit": what the free plan gives or "", "quoted": the article's own sentence stating it}} only if the article states a figure, else null, "included_in": the plan it comes with if the article says, else "", "effort": "minutes", "an afternoon" or "needs a developer", "watch_out": <WATCH>, "link": the official URL if the article names one, else null, "prompt": a starter prompt to paste in, max 300 characters, "" if it takes no text prompts, "steps": 2-4 short steps only if the article says how, in its words, else [], "example": {{"before": ..., "after": ...}} only if the article shows a concrete one, else null}}. <STYLE>

{rules}"""


def _work_card_words(schema: str) -> str:
    """The work_card fields as plain.py's limits describe them, so the prompt asks for what the rules
    keep (braces doubled: the schema is formatted later)."""
    g = work.plain.prompt_guide()
    for key, field in (("<HEADLINE>", "headline"), ("<YOU_GET>", "you_get"), ("<WHAT>", "what_it_does"),
                       ("<USES>", "use_for"), ("<WATCH>", "watch_out"), ("<STYLE>", "style")):
        schema = schema.replace(key, g[field].replace("{", "{{").replace("}", "}}"))
    return schema


SCHEMA = _work_card_words(SCHEMA)

# The importance ladder, for the providers whose window has room for it.
IMPORTANCE_LADDER = """ 8-9 = news a professional has to know today: a leading lab's own model or product launch, a funding round or acquisition above $1B, a law or ruling that changes what companies may do, a safety or security incident at a large provider, a named departure at the top of a major lab. 6-7 = a real but narrower move: a smaller lab's release, a deal in the hundreds of millions, a capability arriving in a tool millions of people use. 3-4 = an incremental product update, a benchmark run, a company's blog post about its own practice, a tutorial, a survey with no new data. 1-2 = a listicle, a rewrite of someone else's story, or a piece with nothing new in it. Judge the news, not the publisher, and do not raise the number because the article sounds excited."""

RULES = """
Three rules that matter more than the rest:
1. Figures. Copy every number, sum, percentage and date exactly as the article writes them. Never convert a currency, never round, never add up figures the article keeps apart, and never fill one in from your own knowledge. A range stays a range, an estimate stays an estimate. If a figure is unclear, leave it out: a summary without a number is fine, a wrong number is not.
2. Named parties. Name only the companies, models and people the article names, spelled as it spells them, and say who did what - who announced, who paid, who is accused, who disagrees, whose figures these are. If the article does not say who, say that it does not say.
3. Voice. Plain English for a busy reader, one idea per sentence, the concrete before the abstract. No marketing words (revolutionary, game-changing, seamless, cutting-edge, unlock, empower, robust, leverage), no "In this article", "The article", "This piece", no throat-clearing opening, no exclamation marks, no addressing the reader, no closing thought about the future unless the article makes it.
"""

RULES_SHORT = """
Three rules that matter more than the rest:
1. Figures. Copy every number, percentage and date exactly as the article writes it. Never convert, round, add up or guess a figure; leave it out instead.
2. Named parties. Name only the companies, models and people the article names, and say who did what - who announced, who paid, whose figures these are.
3. Voice. Plain English, one idea per sentence. No marketing words (revolutionary, game-changing, seamless, unlock, empower), no "In this article", no exclamation marks.
"""

EXAMPLES = """
Two worked examples of the voice. They show wording only: always return the full JSON object described above.

Example 1 - a lab's own launch post, with its own benchmark figures.
"headline": "OpenAI ships GPT-5.5 with a 2M-token window at $10 per million input tokens"
"summary_md": "OpenAI released GPT-5.5 on Tuesday. The model takes up to 2 million tokens of context and costs $10 per million input tokens and $30 per million output tokens. It is in the API now, and OpenAI says ChatGPT subscribers get it next week.\\n\\nOpenAI reports 71% on SWE-bench Verified, against 64% for GPT-5. Those are the company's own figures and no outside group has checked them. Input pricing is unchanged from GPT-5; output is a third higher."
"why_it_matters": "A long-context model at this price changes what teams can put in a single prompt instead of building retrieval around it."
"importance": 9

Example 2 - a report built on unnamed sources: every sentence keeps the source's hedging.
"headline": "Anthropic is reportedly in early talks to raise at a $400 billion valuation"
"summary_md": "Anthropic is in early talks with investors about a round that would value it at about $400 billion, Reuters reported on Monday, citing two people it did not name. No terms have been agreed and the talks may not lead to a deal, according to the report. Anthropic declined to comment.\\n\\nThe figure would be roughly double the $183 billion valuation of the round it closed in September. Nothing in the report is confirmed by the company or by any investor on the record."
"importance": 7

Example 3 - a vendor's how-to, useful but not news.
"headline": "Shopify Magic writes bulk product descriptions for stores on the Basic plan"
"key_points": ["Bulk descriptions cover up to 50 products at a time, Shopify says", "Included in the Basic plan at $39 a month, with no separate AI charge", "Descriptions are drafts: Shopify's help page tells merchants to edit before publishing"]
"importance": 4
"""

EXAMPLES_SHORT = """
The voice, in short (wording only: always return the full JSON object).
Good: "OpenAI released GPT-5.5 on Tuesday. It costs $10 per million input tokens. OpenAI reports 71% on SWE-bench Verified against 64% for GPT-5; those are its own figures."
Good, keeping a report's hedging: "Anthropic is in early talks to raise at about a $400 billion valuation, Reuters reported on Monday, citing two people it did not name."
Bad: "In this article we look at OpenAI's game-changing new model."
"""

ARTICLE = """
Article title: {title}
Source: {source}
Published: {published}

Article text:
\"\"\"
{text}
\"\"\"
"""

# The full prompt, unchanged in shape for anything that formats it with the article in one call.
PROMPT = SCHEMA.replace("{ladder}", IMPORTANCE_LADDER).replace("{rules}", RULES) + ARTICLE


def categories_line() -> str:
    return ", ".join(f'"{k}" ({v})' for k, v in config.CATEGORIES.items())


def input_words(provider: str, local_only: bool = False) -> int:
    """Words of article text sent to a provider: Groq pays for a tokens-per-minute window and the
    local 3B model pays in runner seconds, so both get less."""
    if provider == "ollama" or local_only:
        return config.LOCAL_INPUT_WORDS
    if provider == "groq":
        return config.GROQ_INPUT_WORDS
    return config.LLM_INPUT_WORDS


def build_prompt(row, text: str, provider: str, local_only: bool = False) -> str:
    """The prompt for one article.

    Every provider gets the same fields and the same three rules, because the rest of the pipeline
    reads those blocks. What the narrow windows trade away is the length of the guidance: Groq pays
    for a tokens-per-minute window and the local 3B model has a 4,096-token context, so they get
    the rules and the voice in short form and no worked examples. estimated_tokens() and
    config.PROMPT_TOKEN_BUDGET keep each of them inside its window (test_summaries.py).
    """
    narrow = provider in ("groq", "ollama") or local_only
    schema = SCHEMA.format(categories=categories_line(), ladder="" if narrow else IMPORTANCE_LADDER,
                           rules=RULES_SHORT if narrow else RULES)
    return (schema + (EXAMPLES_SHORT if narrow else EXAMPLES)
            + ARTICLE.format(title=row.title, source=row.source_name,
                             published=row.published_at.isoformat() if row.published_at else "unknown",
                             text=_truncate(text, input_words(provider, local_only))))


def estimated_tokens(prompt: str) -> int:
    """A deliberately generous estimate (words plus punctuation), used to keep a prompt inside a
    provider's window without calling a tokeniser."""
    return int(len(prompt.split()) * 1.35 + len(prompt) / 60)


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


# Providers whose call function counts its own requests (call_mistral, call_cloudflare and
# call_openrouter: they are held by money, neurons or a daily request count that includes failures,
# and are also called from steps that record nothing, howto and simplify). A caller's count for them
# is dropped here so nothing is counted twice; an "exhausted" mark goes through.
SELF_COUNTING = frozenset({"mistral", "cloudflare", "openrouter"})


def record_usage(eng, provider: str, n: int = 1, exhausted: bool = False) -> None:
    from sqlalchemy import insert, update

    if provider in SELF_COUNTING and not exhausted:
        return
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


def _minutes_left_today() -> int:
    now = db.utcnow()
    return 24 * 60 - (now.hour * 60 + now.minute)


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
    runs_left = max(1, -(-_minutes_left_today() * config.RUNS_PER_DAY // (24 * 60)))  # ceil
    return max(0, min(config.MAX_ENRICH_PER_RUN, -(-remaining // runs_left)))


def spare(conn, provider: str) -> int:
    """Requests the day is ahead of pace: what is left minus what the rest of the day needs.

    A quiet night spends little, so by morning the day is ahead and a few calls can go to
    something other than the queue (upgrade.py). A busy day is never ahead, so it never does.
    """
    budget = config.DAILY_BUDGET.get(provider)
    if budget is None:
        return 0
    used, exhausted = usage_today(conn, provider)
    if exhausted:
        return 0
    return int(max(0, budget - used) - budget * (_minutes_left_today() / (24 * 60)))


# ----------------------------------------------------------------- queue order
# Which article the strongest model gets. The providers are tried in order (Gemini, then Ollama
# Cloud, then Groq, then Cloudflare, then OpenRouter, then Mistral, then the local model), each with its share of the run, so whatever stands at
# the front of the queue is what the best model reads. Before this, that was whatever arrived
# first, which on most mornings meant an arXiv listing rather than a lab's own announcement.

# Sources that arrive in floods and rarely lead a story: preprint listings and community posts.
# They still get summarised, by a cheaper provider, unless the web is already reacting to them.
FLOOD_DOMAINS = frozenset({"arxiv.org", "paperswithcode.com", "reddit.com"})
# Primary domains that are not a company's own announcement: a repository, a model card or a
# preprint is a primary source for its own contents, not the news a story is built on.
NOT_ANNOUNCEMENT = FLOOD_DOMAINS | {"github.com", "huggingface.co"}
LISTICLE = re.compile(
    r"^\s*(?:the\s+)?(?:top|best)\s+\d+\b|\b\d+\s+(?:best|top|free|essential|amazing|must-have|ways?|tips|tricks|"
    r"tools|apps|prompts|things|reasons|examples|alternatives)\b|\bhere'?s (?:how|why|what)\b", re.I)
PRIMARY_BONUS = 1.2
FRONT_PAGE_BONUS = 0.7
FOCUS_BONUS = 0.5
AGE_BONUS = 0.9          # per day of waiting, up to AGE_MAX_DAYS: nothing starves
AGE_MAX_DAYS = 2.0       # two days of waiting outweigh every penalty below
FLOOD_PENALTY = 0.6
LISTICLE_PENALTY = 0.4


def _is_primary_row(row) -> bool:
    # arXiv is a primary source for a paper, and GitHub for a repository, but neither is the
    # announcement a story is built on, and both arrive by the hundred; they are scored through
    # their community signal instead.
    domain = (getattr(row, "domain", "") or "").lower()
    return (domain in config.PRIMARY_DOMAINS or getattr(row, "source_type", None) == "primary") and domain not in NOT_ANNOUNCEMENT


def front_page_words(conn) -> set[str]:
    """Distinctive words of the stories on the front page now, from the runner's copy: an article
    that continues one of them is worth the best model, because it lands on a page readers see."""
    from . import cache

    now = db.utcnow()
    live = [s for s in cache.stories(conn).values()
            if s.status == "published" and (now - (db.as_utc(s.first_published_at) or now)).total_seconds() <= 48 * 3600]
    live.sort(key=lambda s: (-(s.score or 0.0), -s.id))
    titles = cache.story_titles(conn, live[:config.ENRICH_FRONT_PAGE_STORIES])
    words: set[str] = set()
    for row in titles.values():
        words.update(keywords(row.headline or "", 6))
    return words


def queue_score(row, now, front: set[str] = frozenset()) -> float:
    """How much the best model is worth on this article. Higher goes first."""
    weight = float(getattr(row, "weight", 1.0) or 1.0)
    score = 0.8 * max(-0.5, min(1.0, weight - 1.0))
    if _is_primary_row(row):
        score += PRIMARY_BONUS
    if getattr(row, "category_hint", None) in config.FOCUS_CATEGORIES:
        score += FOCUS_BONUS
    title = getattr(row, "title", "") or ""
    if front and len(front & set(keywords(title, 6))) >= 2:
        score += FRONT_PAGE_BONUS
    pop = math.log1p(max(getattr(row, "discussion_points", 0) or 0, 0)) + 0.6 * math.log1p(max(getattr(row, "trend_score", 0) or 0, 0))
    score += 0.6 * min(1.0, pop / 5.0)
    if (getattr(row, "domain", "") or "").lower() in FLOOD_DOMAINS and pop < 1.0:
        score -= FLOOD_PENALTY
    if LISTICLE.search(title):
        score -= LISTICLE_PENALTY
    # Fairness: every hour an article waits lifts it, and after two days the wait outweighs every
    # penalty here, so the weakest item still gets its summary while fresh strong ones keep
    # arriving. The lift stops there, so a backlog cannot hold the queue for ever; the gate drops
    # anything older than MAX_ARTICLE_AGE_DAYS, which bounds the wait from the other side.
    waited = (now - (db.as_utc(getattr(row, "created_at", None)) or now)).total_seconds() / 3600
    score += AGE_BONUS * min(AGE_MAX_DAYS, max(0.0, waited) / config.ENRICH_QUEUE_AGE_HOURS)
    return round(score, 4)


def order_queue(rows: list, now, front: set[str] = frozenset()) -> list:
    """The queue, best first; ties go to the article that has waited longest."""
    return sorted(rows, key=lambda r: (-queue_score(r, now, front),
                                       db.as_utc(getattr(r, "created_at", None)).timestamp() if getattr(r, "created_at", None) else 0.0,
                                       r.id))


def pick_queue(conn, limit: int) -> list[int]:
    """The ids this run summarises, best first. Two narrow queries (no article text) over the
    waiting articles: the newest ones, and the oldest, so a backlog cannot be forgotten."""
    a, s = db.articles.c, db.sources.c
    # Only what the score reads: no article text, no dates beyond the one the age rule uses.
    cols = [a.id, a.title, a.domain, a.created_at, a.discussion_points, a.trend_score,
            s.weight, s.source_type, s.category_hint]
    waiting = select(*cols).join(db.sources, a.source_id == s.id).where(a.status == "gated")
    pool = conn.execute(waiting.order_by(a.created_at.desc()).limit(config.ENRICH_QUEUE_POOL)).all()
    rows = {r.id: r for r in pool}
    if len(pool) >= config.ENRICH_QUEUE_POOL:  # more are waiting than the pool holds: keep the oldest in view
        for r in conn.execute(waiting.order_by(a.created_at.asc()).limit(config.ENRICH_QUEUE_OLDEST)).all():
            rows.setdefault(r.id, r)
    if not rows:
        return []
    front = front_page_words(conn)
    return [r.id for r in order_queue(list(rows.values()), db.utcnow(), front)[:limit]]


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


# ----------------------------------------------------------------- Mistral (paid by the token, capped)
# The Free plan's $10 a month is spent by the token, so Mistral is held by money, not by a request
# count: every call's cost (usage tokens x price) is added to a month-to-date total kept in
# llm_usage beside the daily request counts, and no call starts once the total plus what the call
# could cost would pass MISTRAL_MONTHLY_CAP_USD. The month is the UTC calendar month, as Mistral's.
#
# Rows, one per UTC day (llm_usage has no cost column, and a row per day is what it already holds):
#   provider "mistral"          requests = calls that day
#   provider "mistral_microusd" requests = that day's spend in millionths of a dollar
#   provider "mistral_pause"    requests = the minute (since the epoch) a plan-level pause ends
# The month-to-date figures come from one grouped query: three short rows, read once a run.

MISTRAL_SPEND = "mistral_microusd"
MISTRAL_PAUSE = "mistral_pause"
MISTRAL_ROWS = ("mistral", MISTRAL_SPEND, MISTRAL_PAUSE)
MISTRAL_ANSWER_TOKENS = 3000
# What a summary call costs at most, for sizing a run's share: the longest prompt (LLM_INPUT_WORDS of
# article, ~9,500 tokens) and ~1,200 tokens out at $0.15 per million. Measured on 21 Sep 2026: an
# 818-word article took 3,559 tokens in and 469 out, $0.0006.
MISTRAL_TYPICAL_CALL_USD = 0.0016


class SpendCapReached(QuotaExhausted):
    """The month's spend cap would be passed: no more calls until the 1st."""


_mistral: dict = {}      # month-to-date state, loaded from llm_usage once a run (_mistral_state)
_mistral_run: dict = {"calls": 0, "paused": None}  # this run (one process): calls sent, why it sat out


def mistral_reset() -> None:
    """Forget the loaded state and the run's count (each run is a new process; tests call this)."""
    _mistral.clear()
    _mistral_run.update(calls=0, paused=None)


def _month_start(day):
    return day.replace(day=1)


def mistral_usage(conn) -> dict:
    """Mistral's calls today and this month, spend month-to-date and any plan pause, from llm_usage.

    One grouped query over the month's rows (and yesterday's, for a pause set on the last day of a
    month): three short result rows whatever the day of the month."""
    from sqlalchemy import case

    now = db.utcnow()
    today = now.date()
    month = _month_start(today).isoformat()
    since = min(month, (today - timedelta(days=1)).isoformat())
    u = db.llm_usage.c
    rows = conn.execute(
        select(u.provider,
               func.sum(case((u.day == today.isoformat(), u.requests), else_=0)).label("today"),
               func.sum(case((u.day >= month, u.requests), else_=0)).label("month"),
               func.max(u.requests).label("top"))
        .where(u.provider.in_(MISTRAL_ROWS), u.day >= since)
        .group_by(u.provider)).all()
    got = {r.provider: r for r in rows}

    def val(provider, field):
        r = got.get(provider)
        return int(getattr(r, field) or 0) if r else 0

    return {
        "day": today.isoformat(), "month": month[:7],
        "callsToday": val("mistral", "today"), "callsMonth": val("mistral", "month"),
        "spentMicro": val(MISTRAL_SPEND, "month"),
        "pausedUntil": val(MISTRAL_PAUSE, "top") * 60.0,  # epoch seconds; 0 when never paused
    }


def _mistral_state(conn=None) -> dict:
    """The month-to-date state, read once a run and kept current in memory after each call. Read
    again when the UTC day changes, so a run that crosses midnight on the 1st starts the new month.
    A failed read fails closed: no Mistral call is made on a spend figure that could not be read."""
    today = db.utcnow().date().isoformat()
    if _mistral.get("day") != today:
        try:
            if conn is not None:
                fresh = mistral_usage(conn)
            else:
                with db.engine().connect() as c:
                    fresh = mistral_usage(c)
            fresh["error"] = None
        except Exception as exc:  # noqa: BLE001
            log.warning("mistral: month-to-date spend could not be read (%s); not using it", str(exc)[:120])
            fresh = {"day": None, "error": str(exc)[:120], "spentMicro": 0, "pausedUntil": 0.0,
                     "callsToday": 0, "callsMonth": 0, "month": today[:7]}
        _mistral.clear()
        _mistral.update(fresh)
    return _mistral


def mistral_spent_usd(state: dict | None = None) -> float:
    return (state if state is not None else _mistral_state()).get("spentMicro", 0) / 1e6


def mistral_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * config.MISTRAL_PRICE_IN_PER_M + completion_tokens * config.MISTRAL_PRICE_OUT_PER_M) / 1e6


def mistral_block(conn=None, prompt: str | None = None, answer_tokens: int = MISTRAL_ANSWER_TOKENS) -> str | None:
    """Why Mistral may not be called now, or None. With a prompt, the cap check includes what this
    call could cost at most (its prompt plus a full answer), so the cap is never crossed."""
    if not config.MISTRAL_API_KEY:
        return "no key"
    if _mistral_run["paused"]:
        return f"paused for this run ({_mistral_run['paused']})"
    if _mistral_run["calls"] >= config.MISTRAL_MAX_PER_RUN:
        return f"the run's {config.MISTRAL_MAX_PER_RUN} calls are used"
    st = _mistral_state(conn)
    if st.get("error"):
        return "month-to-date spend could not be read"
    if st.get("pausedUntil", 0) > db.utcnow().timestamp():
        return "model not available on this plan (paused until " + datetime.fromtimestamp(st["pausedUntil"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ")"
    worst = mistral_cost(estimated_tokens(prompt), answer_tokens) if prompt is not None else MISTRAL_TYPICAL_CALL_USD
    if mistral_spent_usd(st) + worst > config.MISTRAL_MONTHLY_CAP_USD:
        return f"monthly cap reached (${mistral_spent_usd(st):.2f} of ${config.MISTRAL_MONTHLY_CAP_USD:.2f})"
    return None


def mistral_allowance(conn=None) -> int:
    """Calls this run's enrich step may give Mistral: bounded by the run's cap, the step's own cap,
    and what is left of the month's money at a typical call's cost."""
    if mistral_block(conn):
        return 0
    left_usd = config.MISTRAL_MONTHLY_CAP_USD - mistral_spent_usd()
    return max(0, min(config.MAX_ENRICH_PER_RUN, config.MISTRAL_MAX_PER_RUN - _mistral_run["calls"],
                      int(left_usd // MISTRAL_TYPICAL_CALL_USD)))


def _usage_add(values: dict[str, int], keep_max: bool = False) -> None:
    """Add to (or, with keep_max, raise to) today's llm_usage rows for these providers: one short
    transaction (Mistral's and Cloudflare's own bookkeeping)."""
    from sqlalchemy import insert

    day = _today()
    u = db.llm_usage.c
    with db.engine().begin() as conn:
        have = {r.provider: r for r in conn.execute(
            select(u.id, u.provider, u.requests).where(u.day == day, u.provider.in_(list(values)))).all()}
        for provider, n in values.items():
            row = have.get(provider)
            if row is None:
                conn.execute(insert(db.llm_usage).values(day=day, provider=provider, requests=int(n), exhausted=False))
            elif keep_max:
                conn.execute(update(db.llm_usage).where(u.id == row.id).values(requests=max(int(row.requests or 0), int(n))))
            else:
                conn.execute(update(db.llm_usage).where(u.id == row.id).values(requests=u.requests + int(n)))


def _mistral_charge(cost_usd: float) -> None:
    """Count one answered call and its cost, in memory at once and in llm_usage."""
    micro = max(0, round(cost_usd * 1e6))
    st = _mistral_state()
    st["spentMicro"] = st.get("spentMicro", 0) + micro
    st["callsToday"] = st.get("callsToday", 0) + 1
    st["callsMonth"] = st.get("callsMonth", 0) + 1
    try:
        _usage_add({"mistral": 1, MISTRAL_SPEND: micro})
    except Exception as exc:  # noqa: BLE001 - the in-memory total still holds this run to the cap
        log.warning("mistral: spend not stored (%s)", str(exc)[:120])


def _mistral_pause_for_plan() -> None:
    until = db.utcnow().timestamp() + config.MISTRAL_PLAN_PAUSE_HOURS * 3600
    _mistral_state()["pausedUntil"] = until
    try:
        _usage_add({MISTRAL_PAUSE: int(until // 60)}, keep_max=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("mistral: pause not stored (%s)", str(exc)[:120])


def call_mistral(prompt: str) -> dict:
    """Ministral 8B on Mistral's Free plan. Refuses before sending when the month's spend cap, the
    run's call cap or a pause stands in the way. 429 or 402: Mistral sits out the rest of this run.
    429 with x-ratelimit-limit-req-minute 0: the model is not on this plan, paused for 24 hours."""
    reason = mistral_block(prompt=prompt)
    if reason:
        raise (SpendCapReached if reason.startswith("monthly cap") else ProviderPaused)(f"mistral: {reason}")
    model = config.MISTRAL_MODEL
    _mistral_run["calls"] += 1
    resp = requests.post(
        f"{config.MISTRAL_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.MISTRAL_API_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
            "max_tokens": MISTRAL_ANSWER_TOKENS,
        },
        timeout=_request_timeout(90),
    )
    if resp.status_code == 429 and str(resp.headers.get("x-ratelimit-limit-req-minute", "")).strip() == "0":
        _mistral_pause_for_plan()
        _mistral_run["paused"] = "model not available on this plan"
        log.warning("Mistral model not available on this plan (%s): paused for %g hours", model, config.MISTRAL_PLAN_PAUSE_HOURS)
        raise QuotaExhausted(f"mistral {model}: model not available on this plan")
    if resp.status_code in (401, 402, 403, 429):
        _mistral_run["paused"] = f"http {resp.status_code}"
        raise ProviderPaused(f"mistral {model} http {resp.status_code}: {resp.text[:120]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"mistral {model} http {resp.status_code}: {resp.text[:160]}")
    data = resp.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    # Tokens were spent whether or not the answer parses, so the cost is booked first.
    tokens_in = int(usage.get("prompt_tokens") or estimated_tokens(prompt))
    tokens_out = int(usage.get("completion_tokens") or estimated_tokens(content))
    _mistral_charge(mistral_cost(tokens_in, tokens_out))
    return _parse_json(content)


# ----------------------------------------------------------------- Cloudflare Workers AI (daily neurons)
# The Workers Free plan gives 10,000 "neurons" a day (reset 00:00 UTC), spent by the token. Each
# answer reports what it cost (usage.neurons); the day's total is kept in llm_usage beside the
# request counts, and no call starts once the total plus what the call could cost would pass the
# limit for its purpose: CLOUDFLARE_DAILY_NEURONS for upgrade.py, which Cloudflare is first choice
# for, and CLOUDFLARE_FALLBACK_SHARE of it for the fallback uses (enrich, howto, simplify), which run
# earlier in each run and must not eat the rewrites' neurons.
#
# Rows, one per UTC day:
#   provider "cloudflare"          requests = calls that day
#   provider "cloudflare_neurons"  requests = neurons that day (each answer's figure rounded up)
#   provider "cfpause_<8 hex>"     requests = the minute (since the epoch) a model's 403 pause ends
# Read once a run with one grouped query over today's and yesterday's rows.

CF_CALLS, CF_NEURONS, CF_PAUSE = "cloudflare", "cloudflare_neurons", "cfpause_"
CF_ANSWER_TOKENS = 2000
# Nemotron 3 120B without thinking, measured 21 Sep 2026: 3,572 tokens in and 389 out cost 213.6
# neurons, and 2,645 out (with thinking) 520.5, so ~0.045 a token in and ~0.136 a token out.
CF_NEURONS_PER_IN = 0.045
CF_NEURONS_PER_OUT = 0.136
CF_TYPICAL_CALL_NEURONS = 300.0  # an enrich-size call; a multi-source rewrite is ~450
# Models that think before answering unless told not to; thinking tripled the neurons of a summary
# and, at 3,000 tokens, used the whole answer on it.
CF_THINKING = ("nemotron", "qwen")
_HARMONY = re.compile(r"<\|(?:start|end|message|channel|return|call)\|>(?:assistant|final|analysis)?")

_cf: dict = {}
_cf_run: dict = {"calls": 0, "paused": None, "dead": set()}


def cloudflare_reset() -> None:
    _cf.clear()
    _cf_run.update(calls=0, paused=None, dead=set())


def _cf_pause_key(model: str) -> str:
    import hashlib

    return CF_PAUSE + hashlib.md5(model.encode("utf-8")).hexdigest()[:8]


def cloudflare_usage(conn) -> dict:
    """Cloudflare's calls and neurons today and any model pauses, from llm_usage: one grouped query
    over today's and yesterday's rows (a pause may have been set yesterday)."""
    from sqlalchemy import case, or_

    today = db.utcnow().date()
    u = db.llm_usage.c
    rows = conn.execute(
        select(u.provider,
               func.sum(case((u.day == today.isoformat(), u.requests), else_=0)).label("today"),
               func.max(u.requests).label("top"))
        .where(or_(u.provider.in_((CF_CALLS, CF_NEURONS)), u.provider.like(CF_PAUSE.rstrip("_") + "%")),
               u.day >= (today - timedelta(days=1)).isoformat())
        .group_by(u.provider)).all()
    got = {r.provider: r for r in rows}
    return {
        "day": today.isoformat(),
        "calls": int(getattr(got.get(CF_CALLS), "today", 0) or 0),
        "neurons": int(getattr(got.get(CF_NEURONS), "today", 0) or 0),
        "paused": {p: int(r.top or 0) * 60.0 for p, r in got.items() if p.startswith(CF_PAUSE)},
    }


def _cf_state(conn=None) -> dict:
    """Today's state, read once a run (again when the UTC day changes); fails closed like Mistral's."""
    today = db.utcnow().date().isoformat()
    if _cf.get("day") != today:
        try:
            if conn is not None:
                fresh = cloudflare_usage(conn)
            else:
                with db.engine().connect() as c:
                    fresh = cloudflare_usage(c)
            fresh["error"] = None
        except Exception as exc:  # noqa: BLE001
            log.warning("cloudflare: today's neurons could not be read (%s); not using it", str(exc)[:120])
            fresh = {"day": None, "error": str(exc)[:120], "calls": 0, "neurons": 0, "paused": {}}
        _cf.clear()
        _cf.update(fresh)
    return _cf


def _cf_models() -> list[str]:
    return [config.CLOUDFLARE_AI_MODEL, *config.CLOUDFLARE_AI_FALLBACK_MODELS]


def _cf_open_models(st: dict) -> list[str]:
    now = db.utcnow().timestamp()
    return [m for m in _cf_models() if m not in _cf_run["dead"] and st["paused"].get(_cf_pause_key(m), 0) <= now]


def cloudflare_limit(purpose: str = "fallback") -> float:
    return config.CLOUDFLARE_DAILY_NEURONS * (1.0 if purpose == "upgrade" else config.CLOUDFLARE_FALLBACK_SHARE)


def cloudflare_block(conn=None, purpose: str = "fallback", prompt: str | None = None) -> str | None:
    """Why Cloudflare may not be called now for this purpose, or None."""
    if not (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_AI_TOKEN):
        return "no credentials"
    if _cf_run["paused"]:
        return f"paused for this run ({_cf_run['paused']})"
    if _cf_run["calls"] >= config.CLOUDFLARE_MAX_PER_RUN:
        return f"the run's {config.CLOUDFLARE_MAX_PER_RUN} calls are used"
    st = _cf_state(conn)
    if st.get("error"):
        return "today's neurons could not be read"
    if not _cf_open_models(st):
        return "no model available on this plan"
    worst = (estimated_tokens(prompt) * CF_NEURONS_PER_IN + CF_ANSWER_TOKENS * CF_NEURONS_PER_OUT
             if prompt is not None else CF_TYPICAL_CALL_NEURONS)
    limit = cloudflare_limit(purpose)
    if st["neurons"] + worst > limit:
        return f"daily neurons reached ({st['neurons']:,} of {limit:,.0f} for {purpose})"
    return None


def cloudflare_allowance(conn=None) -> int:
    """Calls this run's enrich step may give Cloudflare, within the fallback share of the day."""
    if cloudflare_block(conn):
        return 0
    left = cloudflare_limit("fallback") - _cf_state(conn)["neurons"]
    return max(0, min(config.MAX_ENRICH_PER_RUN, config.CLOUDFLARE_MAX_PER_RUN - _cf_run["calls"],
                      int(left // CF_TYPICAL_CALL_NEURONS)))


def _cf_charge(neurons: float) -> None:
    whole = max(0, math.ceil(neurons))
    st = _cf_state()
    st["neurons"] = st.get("neurons", 0) + whole
    st["calls"] = st.get("calls", 0) + 1
    try:
        _usage_add({CF_CALLS: 1, CF_NEURONS: whole})
    except Exception as exc:  # noqa: BLE001 - the in-memory total still holds this run to the limit
        log.warning("cloudflare: neurons not stored (%s)", str(exc)[:120])


def _cf_pause_model(model: str) -> None:
    until = db.utcnow().timestamp() + config.CLOUDFLARE_MODEL_PAUSE_HOURS * 3600
    _cf_state()["paused"][_cf_pause_key(model)] = until
    try:
        _usage_add({_cf_pause_key(model): int(until // 60)}, keep_max=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("cloudflare: pause not stored (%s)", str(exc)[:120])


def call_cloudflare(prompt: str, purpose: str = "fallback") -> dict:
    """Workers AI, Nemotron 3 120B first. 403 "not available on the Workers Free plan": that model is
    skipped for 24 hours and the next one tried. 429 (the day's free neurons, or a rate limit) or
    401: Cloudflare sits out the rest of this run. Refuses before sending when the day's neurons for
    this purpose, or the run's call cap, would be passed."""
    reason = cloudflare_block(purpose=purpose, prompt=prompt)
    if reason:
        raise ProviderPaused(f"cloudflare: {reason}")
    url = f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
    last: Exception | None = None
    for model in _cf_open_models(_cf_state()):
        if cloudflare_block(purpose=purpose, prompt=prompt):
            break
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3,
                "response_format": {"type": "json_object"}, "max_tokens": CF_ANSWER_TOKENS}
        if any(t in model for t in CF_THINKING):
            body["chat_template_kwargs"] = {"enable_thinking": False}
        _cf_run["calls"] += 1
        resp = requests.post(url, headers={"Authorization": f"Bearer {config.CLOUDFLARE_AI_TOKEN}"}, json=body,
                             timeout=_request_timeout(120))
        if resp.status_code == 403:
            _cf_pause_model(model)
            log.warning("Cloudflare model %s not available on this plan: skipped for %g hours", model,
                        config.CLOUDFLARE_MODEL_PAUSE_HOURS)
            last = QuotaExhausted(f"cloudflare {model} 403")
            continue
        if resp.status_code in (401, 429):
            _cf_run["paused"] = f"http {resp.status_code}"
            raise ProviderPaused(f"cloudflare http {resp.status_code}: {resp.text[:120]}")
        if resp.status_code == 404:
            _cf_run["dead"].add(model)
            last = QuotaExhausted(f"cloudflare {model} 404")
            continue
        if resp.status_code >= 500:
            last = RuntimeError(f"cloudflare {model} http {resp.status_code}")
            continue
        if resp.status_code >= 400:
            # Often a parameter this model does not take; the next model may.
            last = RuntimeError(f"cloudflare {model} http {resp.status_code}: {resp.text[:120]}")
            continue
        data = resp.json()
        usage = data.get("usage") or {}
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        content = _HARMONY.sub(" ", message.get("content") or "")
        neurons = usage.get("neurons")
        if neurons is None:  # not reported: estimate from the tokens, so the day's total still grows
            neurons = (int(usage.get("prompt_tokens") or estimated_tokens(prompt)) * CF_NEURONS_PER_IN
                       + int(usage.get("completion_tokens") or estimated_tokens(content)) * CF_NEURONS_PER_OUT)
        _cf_charge(float(neurons))
        # A bad answer is not retried on another model: every try costs neurons.
        return _parse_json(content)
    if isinstance(last, QuotaExhausted) or last is None:
        raise ProviderPaused(f"cloudflare: no model available ({last})")
    raise last


def call_cloudflare_upgrade(prompt: str) -> dict:
    """call_cloudflare with the whole day's neurons: what upgrade.py uses."""
    return call_cloudflare(prompt, purpose="upgrade")


# ----------------------------------------------------------------- OpenRouter (free models, daily requests)
# Free models only: an id that does not end in ":free" is never sent, so a typo or a changed setting
# cannot reach a paid model. The account has 50 requests a day, and OpenRouter counts failures too,
# so every request sent is counted, before its answer is read. The fallback uses may send up to
# OPENROUTER_FALLBACK_REQUESTS a day; the top rewrites (upgrade.py) have the whole
# OPENROUTER_DAILY_REQUESTS.
#
# Rows, one per UTC day:
#   provider "openrouter"        requests = requests sent that day (answered or not)
#   provider "openrouter_top"    requests = top rewrites written with it that day (upgrade.py)
#   provider "openrouter_pause"  requests = the minute (since the epoch) a 402 pause ends

OR_CALLS, OR_TOP, OR_PAUSE = "openrouter", "openrouter_top", "openrouter_pause"
OR_ANSWER_TOKENS = 3000
OR_HEADERS = {"HTTP-Referer": "https://digestai.news", "X-Title": "Digest AI"}

_or: dict = {}
_or_run: dict = {"paused": None, "dead": set()}


def openrouter_reset() -> None:
    _or.clear()
    _or_run.update(paused=None, dead=set())


def openrouter_models() -> list[str]:
    """The configured models that are free; anything else is refused, loudly."""
    out = []
    for m in config.OPENROUTER_MODELS:
        if m.endswith(":free"):
            out.append(m)
        elif m not in _or_refused:
            _or_refused.add(m)
            log.warning("openrouter: %s is not a free model id (no ':free'); never sent", m)
    return out


_or_refused: set[str] = set()  # paid ids already reported, so the log says it once


def openrouter_usage(conn) -> dict:
    """OpenRouter's requests and top rewrites today and any pause: one grouped query, three rows."""
    from sqlalchemy import case

    today = db.utcnow().date()
    u = db.llm_usage.c
    rows = conn.execute(
        select(u.provider,
               func.sum(case((u.day == today.isoformat(), u.requests), else_=0)).label("today"),
               func.max(u.requests).label("top"))
        .where(u.provider.in_((OR_CALLS, OR_TOP, OR_PAUSE)), u.day >= (today - timedelta(days=1)).isoformat())
        .group_by(u.provider)).all()
    got = {r.provider: r for r in rows}
    return {
        "day": today.isoformat(),
        "calls": int(getattr(got.get(OR_CALLS), "today", 0) or 0),
        "top": int(getattr(got.get(OR_TOP), "today", 0) or 0),
        "pausedUntil": int(getattr(got.get(OR_PAUSE), "top", 0) or 0) * 60.0,
    }


def _or_state(conn=None) -> dict:
    today = db.utcnow().date().isoformat()
    if _or.get("day") != today:
        try:
            if conn is not None:
                fresh = openrouter_usage(conn)
            else:
                with db.engine().connect() as c:
                    fresh = openrouter_usage(c)
            fresh["error"] = None
        except Exception as exc:  # noqa: BLE001
            log.warning("openrouter: today's requests could not be read (%s); not using it", str(exc)[:120])
            fresh = {"day": None, "error": str(exc)[:120], "calls": 0, "top": 0, "pausedUntil": 0.0}
        _or.clear()
        _or.update(fresh)
    return _or


def openrouter_limit(purpose: str = "fallback") -> int:
    return config.OPENROUTER_DAILY_REQUESTS if purpose == "upgrade" else min(
        config.OPENROUTER_FALLBACK_REQUESTS, config.OPENROUTER_DAILY_REQUESTS)


def openrouter_block(conn=None, purpose: str = "fallback") -> str | None:
    """Why OpenRouter may not be asked now for this purpose, or None."""
    if not config.OPENROUTER_API_KEY:
        return "no key"
    if _or_run["paused"]:
        return f"paused for this run ({_or_run['paused']})"
    st = _or_state(conn)
    if st.get("error"):
        return "today's requests could not be read"
    if st["pausedUntil"] > db.utcnow().timestamp():
        return "payment required (paused until " + datetime.fromtimestamp(st["pausedUntil"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ")"
    if not [m for m in openrouter_models() if m not in _or_run["dead"]]:
        return "no free model left this run"
    if st["calls"] >= openrouter_limit(purpose):
        return f"daily requests reached ({st['calls']} of {openrouter_limit(purpose)} for {purpose})"
    return None


def openrouter_allowance(conn=None) -> int:
    """Articles this run's enrich step may give OpenRouter: its fallback requests left today, spread
    over the runs still to come so the morning does not take the whole day's."""
    if openrouter_block(conn):
        return 0
    left = openrouter_limit("fallback") - _or_state(conn)["calls"]
    runs_left = max(1, -(-_minutes_left_today() * config.RUNS_PER_DAY // (24 * 60)))
    return max(0, min(config.MAX_ENRICH_PER_RUN, -(-left // runs_left)))


def openrouter_top_left(conn=None) -> int:
    """Top rewrites OpenRouter may still write today (upgrade.py)."""
    if openrouter_block(conn, purpose="upgrade"):
        return 0
    return max(0, config.OPENROUTER_UPGRADE_DAILY - _or_state(conn)["top"])


def openrouter_count_top() -> None:
    st = _or_state()
    st["top"] = st.get("top", 0) + 1
    try:
        _usage_add({OR_TOP: 1})
    except Exception as exc:  # noqa: BLE001
        log.warning("openrouter: top rewrite not counted (%s)", str(exc)[:120])


def _or_count_request() -> None:
    st = _or_state()
    st["calls"] = st.get("calls", 0) + 1
    try:
        _usage_add({OR_CALLS: 1})
    except Exception as exc:  # noqa: BLE001 - the in-memory count still holds this run to the limit
        log.warning("openrouter: request not counted (%s)", str(exc)[:120])


def _or_pause_day() -> None:
    until = db.utcnow().timestamp() + config.OPENROUTER_PAUSE_HOURS * 3600
    _or_state()["pausedUntil"] = until
    try:
        _usage_add({OR_PAUSE: int(until // 60)}, keep_max=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("openrouter: pause not stored (%s)", str(exc)[:120])


def _or_error(resp) -> tuple[int, str] | None:
    """(code, message) of an error, whether it came as an HTTP status or inside a 200 body (an
    overloaded upstream answers 200 with an "error" object), or None."""
    body = None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        pass
    err = body.get("error") if isinstance(body, dict) else None
    if resp.status_code >= 400 or err:
        code = resp.status_code if resp.status_code >= 400 else 0
        if isinstance(err, dict):
            try:
                code = int(err.get("code") or code)
            except (TypeError, ValueError):
                pass
            return code or 500, str(err.get("message") or "")[:160]
        return code or 500, (resp.text or "")[:160]
    choice = (body.get("choices") or [{}])[0] if isinstance(body, dict) else {}
    if isinstance(choice.get("error"), dict):  # a provider failing mid-answer
        e = choice["error"]
        return int(e.get("code") or 500) if str(e.get("code") or "").isdigit() else 500, str(e.get("message") or "")[:160]
    return None


def call_openrouter(prompt: str, purpose: str = "fallback") -> dict:
    """OpenRouter's free models in order. 429 or 503 (as a status or in the body): that model is not
    asked again this run and the next one is. 402: payment would be needed, so OpenRouter is skipped
    for 24 hours. Every request counts against the day's allowance."""
    reason = openrouter_block(purpose=purpose)
    if reason:
        raise ProviderPaused(f"openrouter: {reason}")
    last: Exception | None = None
    for model in openrouter_models():
        if model in _or_run["dead"]:
            continue
        if not model.endswith(":free"):  # openrouter_models() already refuses these; never send one
            continue
        if openrouter_block(purpose=purpose):
            break
        _or_count_request()  # counted before the answer: OpenRouter counts failures too
        resp = requests.post(
            f"{config.OPENROUTER_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", **OR_HEADERS},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3,
                  "response_format": {"type": "json_object"}, "max_tokens": OR_ANSWER_TOKENS,
                  "reasoning": {"enabled": False}},
            timeout=_request_timeout(180),
        )
        err = _or_error(resp)
        if err:
            code, message = err
            if code == 402:
                _or_pause_day()
                _or_run["paused"] = "402 payment required"
                log.warning("openrouter: 402 payment required (%s); skipped for %g hours", message, config.OPENROUTER_PAUSE_HOURS)
                raise ProviderPaused(f"openrouter 402: {message}")
            if code in (401, 403):
                _or_run["paused"] = f"http {code}"
                raise ProviderPaused(f"openrouter {code}: {message}")
            _or_run["dead"].add(model)  # 429, 503, 404, anything else: the next model, not this one again
            last = RuntimeError(f"openrouter {model} {code}: {message}")
            log.info("openrouter %s answered %s (%s); trying the next model", model, code, message[:80])
            continue
        content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            return _parse_json(content)
        except Exception as exc:  # noqa: BLE001 - another model usually manages valid JSON
            last = RuntimeError(f"openrouter {model} gave no usable JSON: {str(exc)[:80]}")
            _or_run["dead"].add(model)
    if last is None or all(m in _or_run["dead"] for m in openrouter_models()):
        raise ProviderPaused(f"openrouter: no free model answered ({last})")
    raise last


def call_openrouter_upgrade(prompt: str) -> dict:
    """call_openrouter with the whole day's requests: what upgrade.py's top rewrites use."""
    return call_openrouter(prompt, purpose="upgrade")


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
    r"accus(?:es|ed)|warns?|argues?|debates?|whether|sees?|predicts?|expects?|estimates?|forecasts?|believes?|opinion:?)\b", re.I)
QUESTION = re.compile(r"\?")


def title_is_question(title: str) -> bool:
    """The title asks rather than states. "Where Are AI Agents Trading? Data Shows Grok Leading" asks
    and then answers: the statement after the question mark is what the article claims."""
    if not QUESTION.search(title):
        return False
    after = re.sub(r"[^\w\s]", " ", title[title.rfind("?") + 1:]).split()
    return len(after) <= 2


def title_hedges(title: str | None) -> bool:
    """The source's own title frames its claim as a question, a possibility or an allegation."""
    return bool(title) and bool(title_is_question(title) or SOURCE_HEDGE.search(title))


def headline_hedged(title: str | None, headline: str | None) -> bool:
    """True when the source title hedges (a question, may/could/reportedly/allegedly/according to)
    and our headline keeps neither a hedge nor an attribution, stating the claim as fact. Rewriting is
    not something a cheap check can do, so the flag is reported (export.py, quality.py), not fixed."""
    if not title or not headline or title.strip() == headline.strip():
        return False
    return title_hedges(title) and not (QUESTION.search(headline) or HEADLINE_HEDGE.search(headline))


# A list marker the model put in front of a paragraph or a key point ("- Meta shares rose 10%"):
# the page shows it as text, so it comes off.
_BULLET = re.compile(r"^[ \t]*(?:[-*•–]|\d{1,2}[.)])[ \t]+", re.M)


def _unbullet(text: str) -> str:
    return _BULLET.sub("", text).strip()


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
    # AI at Work (/work): validated and clamped in work.py, which drops a card that names no tool or
    # carries no honest caveat, and by rules a developer tool or a course, so no half-card and no
    # infrastructure product can reach a page. Why a card was kept out is counted, not stored.
    work_card, work_dropped = work.screen_card(result.get("work_card"))
    if work_card:
        # Written to the plain-words prompt and rules: the simplify pass (simplify.py) leaves it be.
        work_card["simplified"] = True
    category = str(result.get("category", "")).strip().lower()
    if category not in cats:
        category = category_hint if category_hint in cats else "models"
    headline = str(result.get("headline") or row.title).strip()[:160]
    # Hype words, clickbait frames and shouting come out (checks.py, rules only); a rewrite that
    # would break the headline falls back to the source's title, then to the answer as written.
    entities = result.get("entities") if isinstance(result.get("entities"), dict) else {}
    headline, headline_rules = checks.discipline_headline(headline, getattr(row, "title", None), checks.entity_names(entities))
    key_points = [_unbullet(str(k)) for k in _as_list(result.get("key_points")) if _unbullet(str(k))][:3]
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
        "work_card": work_card,
        "work_dropped": work_dropped,
        "headline": headline,
        "summary_md": _unbullet(str(result.get("summary_md") or "")),
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
        # What the headline rules changed, for the run's stats (not stored).
        "headline_rules": headline_rules,
    }


# ----------------------------------------------------------------- the check before storing

CAUGHT_LISTED = 6  # items carried in the run's stats for the dashboard


def verify(eng, row, source_text: str, clean: dict, used, budgets: dict, spent: dict, check_stats: dict) -> tuple[dict, dict | None]:
    """Check the answer against the article and, if something in it is not there, ask once more
    and then keep only the supported part. `used` is (provider function, prompt, provider name) of
    the call that produced the answer, or None for the heuristic path.

    The check itself is free (checks.py is rules only). The one retry costs a request, so it is
    bounded per run and only taken while the provider has budget and the step has time left.

    The AI at Work card's teaching fields (steps, example, the plan it comes with, the outcome
    headline) are grounded in the article whatever CHECK_SUMMARIES says: they are what a model is
    most tempted to invent, and a reader acts on them (work.ground_card).
    """
    safe, note = _verify(eng, row, source_text, clean, used, budgets, spent, check_stats)
    if safe.get("work_card"):
        safe = {**safe, "work_card": work.ground_card(safe["work_card"], source_text)}
    return safe, note


def _verify(eng, row, source_text: str, clean: dict, used, budgets: dict, spent: dict, check_stats: dict) -> tuple[dict, dict | None]:
    if not config.CHECK_SUMMARIES:
        return clean, None
    check_stats["checked"] += 1
    rep = checks.report(clean, source_text)
    if not checks.items(rep):
        return clean, None
    check_stats["flagged"] += 1
    retried = False
    if used is not None and check_stats["retried"] < config.CHECK_RETRY_MAX_PER_RUN and _time_left() > 45:
        fn, prompt, provider = used
        if spent.get(provider, 0) < budgets.get(provider, 0):
            try:
                again = fn(checks.stricter_prompt(prompt, rep))
                spent[provider] = spent.get(provider, 0) + 1
                check_stats["retried"] += 1
                retried = True
                if provider in budgets and provider != "ollama":
                    record_usage(eng, provider, 1)
                second = _clean(again, row, clean.get("category"))
                second_rep = checks.report(second, source_text)
                if not checks.items(second_rep):
                    log.info("#%s: the stricter retry dropped %s", row.id, ", ".join(checks.items(rep))[:120])
                    return second, checks.note(row, rep, [], True)
                clean, rep = second, second_rep
            except Exception as exc:  # noqa: BLE001 - a failed retry leaves the first answer to be repaired
                log.warning("stricter retry failed on #%s: %s", row.id, str(exc)[:120])
    safe, changed = checks.safer(clean, rep, row, source_text)
    if changed:
        check_stats["edited"] += 1
        # A headline put back to the source's own wording cannot be dropping the source's hedge.
        safe["hedged"] = headline_hedged(getattr(row, "title", None), safe.get("headline"))
    log.info("#%s: the article does not contain %s; %s", row.id, ", ".join(checks.items(rep))[:120],
             "; ".join(changed) or "left as written")
    return safe, checks.note(row, rep, changed, retried)


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
        # Cloudflare Workers AI, within the share of its daily neurons that upgrade.py leaves over.
        if config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_AI_TOKEN:
            budgets["cloudflare"] = cloudflare_allowance(conn)
            if budgets["cloudflare"] > 0:
                providers.append((f"cloudflare:{config.CLOUDFLARE_AI_MODEL}", call_cloudflare))
            else:
                stats["cloudflare"] = cloudflare_block(conn) or "no share this run"
        # OpenRouter's free models, within the requests a day the top rewrites leave over.
        if config.OPENROUTER_API_KEY:
            budgets["openrouter"] = openrouter_allowance(conn)
            if budgets["openrouter"] > 0:
                providers.append((f"openrouter:{(openrouter_models() or ['none'])[0]}", call_openrouter))
            else:
                stats["openrouter"] = openrouter_block(conn) or "no share this run"
        # Mistral fills the gaps the free providers leave, before the weak local model. It is paid
        # by the token: its share is bounded by the month's spend cap and the run's call cap.
        if config.MISTRAL_API_KEY:
            budgets["mistral"] = mistral_allowance(conn)
            if budgets["mistral"] > 0:
                providers.append((f"mistral:{config.MISTRAL_MODEL}", call_mistral))
            else:
                stats["mistral"] = mistral_block(conn) or "no share this run"
    keyed = bool(config.GEMINI_API_KEY or config.GROQ_API_KEY or config.OLLAMA_API_KEY or config.MISTRAL_API_KEY
                 or config.OPENROUTER_API_KEY or (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_AI_TOKEN))
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
    spent: dict[str, int] = {}
    caught: list[dict] = []
    check_stats = {"checked": 0, "flagged": 0, "retried": 0, "edited": 0}

    with eng.connect() as conn:
        # Two steps, so the scarce best model reads the articles most likely to lead a story
        # (queue_score) rather than whatever arrived first: rank the waiting articles on narrow
        # columns, then read the text of the chosen ones only.
        chosen = pick_queue(conn, limit)
        rows = []
        if chosen:
            a = db.articles.c
            # substr: no more text than the longest prompt can carry is ever read.
            found = {r.id: r for r in conn.execute(
                select(a.id, a.title, a.description, func.substr(a.content_text, 1, config.ENRICH_TEXT_CHARS).label("content_text"),
                       a.published_at, db.sources.c.category_hint, db.sources.c.name.label("source_name"), db.sources.c.weight)
                .join(db.sources, a.source_id == db.sources.c.id)
                .where(a.id.in_(chosen))).all()}
            rows = [found[i] for i in chosen if i in found]
    stats["queued"] = len(rows)

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
        result, model_used, used_fn = None, "heuristic", None
        for name, fn in list(providers):
            provider = name.split(":")[0]
            if provider in budgets and spent.get(provider, 0) >= budgets[provider]:
                continue  # this provider's share for the run is spent; try the next one
            # Shorter input and shorter examples for the CPU model (speed) and for Groq (its
            # tokens-per-minute window); the rest, Mistral's 128k-token model included, get the full prompt.
            prompt = build_prompt(row, text, provider, local_only)
            try:
                result = fn(prompt)
                model_used = name
                used_fn = (fn, prompt, provider)
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
        # Every figure and name is checked against the article before anything is stored. The
        # title and the feed's own description count as source material: a figure in the headline
        # ("Last Week in AI #343") often appears nowhere else.
        source_text = "\n".join(x for x in (row.title, row.description, text) if x)
        clean, note = verify(eng, row, source_text, clean, used_fn, budgets, spent, check_stats)
        if note:
            caught.append(note)
        with eng.begin() as conn:
            if not clean["is_ai_news"]:
                stats["rejected"] += 1
                conn.execute(update(db.articles).where(db.articles.c.id == row.id)
                             .values(status="rejected", reject_reason="llm: not ai news", enrich_model=model_used))
                continue
            stats["enriched"] += 1
            if clean["work_card"]:
                stats["work_cards"] = stats.get("work_cards", 0) + 1
            elif clean.get("work_dropped"):
                key = f"work_dropped_{clean['work_dropped']}"
                stats[key] = stats.get(key, 0) + 1
            if clean.get("headline_rules"):
                stats["headline_rules"] = stats.get("headline_rules", 0) + 1
                log.info("headline rules on #%s (%s): %r", row.id, ", ".join(clean["headline_rules"]), clean["headline"][:90])
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
    if check_stats["checked"]:
        stats["checks"] = {**check_stats, "caught": caught[:CAUGHT_LISTED]}
    return stats
