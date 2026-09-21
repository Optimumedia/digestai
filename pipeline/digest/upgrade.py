"""Step: a better summary for the stories that turned out to matter.

An article is summarised the moment it arrives, before anything is known about it: whether other
outlets will follow, whether readers will care, whether it will lead the front page. The best
model is scarce, so most articles are summarised by a smaller one, and now and then the story that
grows out of one of those is the day's biggest.

This step goes back. Once a story has proved itself - several independent sources, a high score, or
readers engaging with it - its digest is written again with the strongest provider that has budget
to spare. When the story carries three or more independent sources, the digest is written from the
best few of them together, so it says what the sources agree on and where they differ, instead of
paraphrasing whichever article happened to arrive first.

What holds it in check:
- it only runs on allowance the day is ahead of pace on (enrich.spare), so the queue always comes
  first, and it stands aside entirely while articles are waiting to be summarised; Mistral, the
  fallback after the strong providers, is held by its monthly spend cap and per-run call cap instead;
- at most UPGRADE_MAX_PER_RUN stories per run and UPGRADE_DAILY_MAX a day, counted in llm_usage;
- a story is never written twice for the same material: the article's enrich_model records how many
  sources the upgrade used, and only UPGRADE_NEW_SOURCES more make it worth writing again;
- nothing here touches first_published_at or updated_at, so an improved summary never makes an old
  story look new (the site's "new" comes from first_published_at);
- candidates come from the runner's copy of the rows (cache.py), so a run with nothing to upgrade
  reads nothing beyond the article text of what it actually rewrites.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select, update

from . import cache, checks, config, db, enrich
from .textutil import word_count

log = logging.getLogger("digest.upgrade")

MULTI_PROMPT = """You are the news editor of Digest AI, a site that covers artificial intelligence.
Below are {n} articles from different sources about the same event, the first one from the source we lead with.
Write the story's digest from all of them together and return ONLY a JSON object with these fields:

- "headline": a plain, specific news headline for the story as a whole, under 90 characters: subject, verb, object - name the company or person and the thing (product, model, deal, ruling), with the key figure if the sources agree on one. No source name, no hype words (revolutionizes, game-changer, unleashes), no teasers ("here's why", "you won't believe", "everything you need to know"), no exclamation marks, no words in capitals, at most one colon, and a question mark only when the sources themselves ask the question. Keep the hedging the sources use: if what happened is only reported, alleged or possible, the headline must say so ("reportedly", "according to", "may"). If the sources disagree about the central fact, the headline states the part they agree on.
- "summary_md": an original 150-300 word digest in 2-3 short paragraphs, plain Markdown, in your own words. The first paragraph is what the sources agree happened, with the figures and names they share. The second says where they differ, or what only one of them reports, and names that source ("Only The Information reports the round is oversubscribed"; "Reuters puts the figure at $3 billion, TechCrunch at $3.5 billion"). If they agree on everything, say that the reporting is consistent and add the context a busy reader needs. Do not copy sentences from the articles, and do not start with "The article".
- "key_points": exactly 3 bullet strings, each max 25 words, the most important concrete facts. If the sources differ on a fact, one of the three says so.
- "why_it_matters": max 60 words on the significance for the AI industry or the public.
- "agree": one sentence, max 40 words: what every one of these articles reports.
- "differ": a list of 0 to 3 short sentences, each naming the outlet and what it alone reports or where its figure or account differs from the others ("Reuters puts the round at $3 billion; TechCrunch says $3.5 billion"). An empty list when the reporting is consistent. Never invent a disagreement: differences of wording or emphasis do not count.
- "entities": {{"companies": [...], "models": [...], "people": [...]}} with proper names actually mentioned, max 6 each.
- "importance": integer 1-10. 8-9 = news a professional has to know today (a leading lab's launch, a deal above $1B, a law or ruling, a safety incident at a large provider). 5 = routine industry news. 3-4 = an incremental update, a benchmark run, a tutorial. Several outlets covering one event does not by itself make it important.

Three rules that matter more than the rest:
1. Figures. Copy every number, sum, percentage and date exactly as one of the articles writes it. Never convert a currency, never round, never add figures together, and never fill one in from your own knowledge. Where two articles give different figures, give both and say who says which.
2. Named parties. Name only the companies, models and people the articles name, spelled as they spell them, and say who did what - who announced, who paid, who is accused, who disagrees, whose figures these are.
3. Voice. Plain English for a busy reader, one idea per sentence. No marketing words (revolutionary, game-changing, seamless, cutting-edge, unlock, empower), no "In this article", "The article", no throat-clearing opening, no exclamation marks.

{articles}
"""

ARTICLE_BLOCK = """
--- Article {i} of {n}: {source} ({domain}), published {published}
Title: {title}

{text}
"""

# enrich_model carries what was done to the article: "gemini:gemini-3.8-flash+u4" means the summary
# was written again with Gemini from 4 independent sources. String(60) in the schema, so it is cut.
MARK = re.compile(r"\+u(\d+)$")


def mark(model: str, sources: int) -> str:
    return f"{MARK.sub('', model or 'unknown')}+u{max(1, int(sources))}"[:60]


def upgraded_with(model: str | None) -> int | None:
    """How many sources the last upgrade of this article used, or None if it was never upgraded."""
    m = MARK.search(model or "")
    return int(m.group(1)) if m else None


def provider_of(model: str | None) -> str:
    return (model or "heuristic").split(":")[0].split("+")[0]


def should_upgrade(*, sources: int, score: float, engagement: float, enrich_model: str | None) -> bool:
    """Whether this story's digest is worth writing again.

    Two reasons, both bounded. A summary from a weak provider on a story that proved important is
    rewritten by a strong one. A story with several independent sources gets a digest written from
    those sources together, however good the single-article summary was. Neither happens twice for
    the same material: an upgrade records the number of sources it used.
    """
    done = upgraded_with(enrich_model)
    if done is not None and sources < done + config.UPGRADE_NEW_SOURCES:
        return False
    if sources >= config.UPGRADE_MIN_SOURCES:
        return True  # worth a multi-source digest whatever wrote the first one
    if done is not None:
        return False
    mattered = score >= config.UPGRADE_MIN_SCORE or engagement > 0
    return mattered and provider_of(enrich_model) not in config.STRONG_PROVIDERS


def pick_articles(lead_id: int | None, members: list, limit: int) -> list:
    """The articles a multi-source digest reads: the lead first, then one per further publisher,
    the most substantial first. One article per domain, so three copies of one wire story do not
    look like three sources."""
    lead = next((m for m in members if m.id == lead_id), None)
    out = [lead] if lead is not None else []
    seen = {(lead.domain or "").lower()} if lead is not None else set()
    rest = sorted((m for m in members if m is not lead),
                  key=lambda m: (-(m.importance or 0), -(m.discussion_points or 0), -(m.word_count or 0), m.id))
    for m in rest:
        domain = (m.domain or "").lower()
        if domain in seen:
            continue
        seen.add(domain)
        out.append(m)
        if len(out) >= limit:
            break
    return out


def independent_sources(members: list) -> int:
    return len({(m.domain or str(m.id)).lower() for m in members})


def candidates(conn, now) -> list[SimpleNamespace]:
    """Stories worth rewriting, best first. Mirror rows only: no article text is read here."""
    stories = cache.stories(conn)
    arts = cache.articles(conn)
    members: dict[int, list] = {}
    for a in arts.values():
        if a.story_id and a.status in ("published", "overflow"):
            members.setdefault(a.story_id, []).append(a)
    since = now - timedelta(hours=config.UPGRADE_LOOKBACK_HOURS)
    out = []
    for s in stories.values():
        if s.status != "published" or (db.as_utc(s.first_published_at) or now) < since:
            continue
        shown = sorted((m for m in members.get(s.id, []) if m.status == "published"), key=lambda m: m.id)
        if not shown:
            continue
        lead = arts.get(s.lead_article_id) or shown[0]
        sources = independent_sources(shown)
        engagement = sum(float(m.engagement or 0.0) for m in shown)
        if not should_upgrade(sources=sources, score=float(s.score or 0.0), engagement=engagement,
                              enrich_model=lead.enrich_model):
            continue
        out.append(SimpleNamespace(story=s, lead=lead, members=shown, sources=sources, score=float(s.score or 0.0)))
    out.sort(key=lambda c: (-c.score, -c.sources, -c.story.id))
    return out


def _providers(conn) -> list[tuple[str, object]]:
    """The strong providers with room, in STRONG_PROVIDERS order (Cloudflare first, on its daily
    neurons; Gemini and Ollama Cloud when they are ahead of their daily pace), then Mistral as the
    fallback when the month's spend cap and the run's call cap leave room (enrich.mistral_block)."""
    out = []
    for name in config.STRONG_PROVIDERS:
        if name == "cloudflare":
            # Held by its daily neurons, not by a request budget: the whole day's are for this step.
            if enrich.cloudflare_block(conn, purpose="upgrade") is None:
                out.append((f"cloudflare:{config.CLOUDFLARE_AI_MODEL}", enrich.call_cloudflare_upgrade))
            continue
        if name == "gemini" and config.GEMINI_API_KEY:
            fn, model = enrich.call_gemini, config.GEMINI_MODEL
        elif name == "cloud" and config.OLLAMA_API_KEY:
            fn, model = enrich.call_ollama_cloud, config.OLLAMA_CLOUD_MODEL
        else:
            continue
        if enrich.spare(conn, name) > 0 and enrich.allowance(conn, name) > 0:
            out.append((f"{name}:{model}", fn))
    if config.MISTRAL_API_KEY and "mistral" not in config.STRONG_PROVIDERS and enrich.mistral_block(conn) is None:
        out.append((f"mistral:{config.MISTRAL_MODEL}", enrich.call_mistral))
    return out


def source_notes(result: dict, rows: list, source_text: str) -> dict | None:
    """The agree/differ block, or None. A "differ" line must name one of the outlets in the story and
    every figure in it must be in the articles: a model asked for differences tends to find some."""
    if not isinstance(result, dict):
        return None
    names = set()
    for r in rows:
        for n in (getattr(r, "source_name", None), getattr(r, "domain", None)):
            n = (n or "").lower().removeprefix("www.")
            if n:
                names.add(n)
                names.add(n.split(".")[0])
    names = {n for n in names if len(n) >= 3}
    differ = []
    for line in enrich._as_list(result.get("differ")):
        line = " ".join(str(line).split())[:240]
        if len(line) < 25 or not any(n in line.lower() for n in names):
            continue
        if checks.unsupported_figures(line, source_text):
            continue
        differ.append(line)
    agree = " ".join(str(result.get("agree") or "").split())[:300]
    if checks.unsupported_figures(agree, source_text):
        agree = ""
    if not differ and not agree:
        return None
    return {"agree": agree, "differ": differ[:3]}


def _clean_multi(result: dict, lead_title: str | None) -> dict:
    """The summary fields of a multi-source answer. The article's model, funding and work-card
    blocks are not asked for here, so they are not touched by this step."""
    if not isinstance(result, dict):
        result = {}
    points = [str(k).strip() for k in enrich._as_list(result.get("key_points")) if str(k).strip()][:3]
    ents = result.get("entities") if isinstance(result.get("entities"), dict) else {}
    entities = {k: [str(v).strip() for v in enrich._as_list(ents.get(k)) if str(v).strip()][:6]
                for k in ("companies", "models", "people")}
    try:
        importance = max(1, min(10, int(result.get("importance", 5))))
    except (TypeError, ValueError):
        importance = 5
    headline, _ = checks.discipline_headline(str(result.get("headline") or lead_title or "").strip()[:160], lead_title)
    return {
        "headline": headline,
        "summary_md": str(result.get("summary_md") or "").strip(),
        "key_points": points,
        "why_it_matters": str(result.get("why_it_matters") or "").strip()[:600],
        "entities": entities,
        "importance": importance,
    }


def build_multi_prompt(rows: list[SimpleNamespace]) -> str:
    """rows: the chosen articles, lead first, each with title, source/domain, published_at, text."""
    blocks = []
    for i, r in enumerate(rows, 1):
        blocks.append(ARTICLE_BLOCK.format(
            i=i, n=len(rows), source=getattr(r, "source_name", None) or r.domain or "unknown",
            domain=r.domain or "", published=r.published_at.isoformat() if getattr(r, "published_at", None) else "unknown",
            title=r.title or "", text=enrich._truncate(r.text or "", config.UPGRADE_ARTICLE_WORDS)))
    return MULTI_PROMPT.format(n=len(rows), articles="\n".join(blocks))


def _story_rows(conn, cand, wanted: int) -> list[SimpleNamespace]:
    """The chosen articles with their text, from the runner's copy where it has them."""
    picked = pick_articles(cand.lead.id, cand.members, wanted)
    text = cache.article_text(conn, picked)
    bodies = cache.article_bodies(conn, picked)
    out = []
    for m in picked:
        t = text.get(m.id)
        if t is None:
            continue
        body = bodies.get(m.id) or t.description or ""
        if word_count(body) < 40 and m.id != cand.lead.id:
            continue  # a stub adds no source; the lead is kept whatever it carries
        out.append(SimpleNamespace(id=m.id, domain=m.domain, published_at=m.published_at, title=t.title,
                                   source_name=m.domain, text=body, description=t.description))
    return out


def run() -> dict:
    stats: dict = {"upgraded": 0, "multi": 0, "single": 0, "candidates": 0, "errors": 0}
    if not config.UPGRADE_SUMMARIES:
        return {"off": True}
    eng = db.engine()
    now = db.utcnow()
    with eng.connect() as conn:
        waiting = conn.execute(select(func.count()).select_from(db.articles)
                               .where(db.articles.c.status == "gated")).scalar() or 0
        if waiting > config.UPGRADE_MAX_WAITING:
            return {**stats, "skipped": "articles still waiting for a first summary"}
        done_today, _ = enrich.usage_today(conn, "upgrade")
        room = min(config.UPGRADE_MAX_PER_RUN, config.UPGRADE_DAILY_MAX - done_today)
        providers = _providers(conn)
        if room <= 0 or not providers:
            return {**stats, "skipped": "no spare allowance" if not providers else "daily upgrade limit reached"}
        picked = candidates(conn, now)
        stats["candidates"] = len(picked)
        jobs = []
        strong = any(name.split(":")[0] in config.STRONG_PROVIDERS for name, _fn in providers)
        if not strong:
            # Only the fallback has room: it writes multi-source digests only (see below), so the
            # single-source candidates' text is not read.
            picked = [c for c in picked if c.sources >= config.UPGRADE_MIN_SOURCES]
        for cand in picked[:room]:
            multi = cand.sources >= config.UPGRADE_MIN_SOURCES
            rows = _story_rows(conn, cand, config.UPGRADE_MULTI_ARTICLES if multi else 1)
            if rows:
                jobs.append((cand, rows, multi and len(rows) >= 2))
    if not jobs:
        return stats

    enrich._deadline = time.monotonic() + config.UPGRADE_TIME_BUDGET_SECONDS
    written: list[int] = []
    written_articles: list[int] = []
    try:
        for cand, rows, multi in jobs:
            if enrich._time_left() < 45:
                stats["stopped_for_time"] = True
                break
            lead_row = rows[0]
            if multi:
                prompt = build_multi_prompt(rows)
            else:
                prompt = enrich.build_prompt(SimpleNamespace(title=lead_row.title, source_name=lead_row.source_name,
                                                             published_at=lead_row.published_at),
                                             lead_row.text, "gemini")
            result, model_used, used = None, None, None
            for name, fn in providers:
                provider = name.split(":")[0]
                if provider not in config.STRONG_PROVIDERS and not multi:
                    # A single-article rewrite exists to put a stronger model on the story; the
                    # fallback (Mistral's 8B model) is not one, so it only writes multi-source digests.
                    continue
                try:
                    result = fn(prompt)
                    model_used, used = name, (fn, prompt, provider)
                    enrich.record_usage(eng, provider, 1)
                    break
                except Exception as exc:  # noqa: BLE001 - the next provider, or next run
                    stats["errors"] += 1
                    log.warning("%s could not rewrite story #%s: %s", name, cand.story.id, str(exc)[:160])
            if result is None:
                continue
            source_text = "\n\n".join("\n".join(x for x in (r.title, r.description, r.text) if x) for r in rows)
            article_row = SimpleNamespace(id=lead_row.id, title=lead_row.title)
            if multi:
                clean = _clean_multi(result, lead_row.title)
                if word_count(clean["summary_md"]) < 60:
                    log.warning("story #%s: the multi-source answer was too short to use", cand.story.id)
                    continue
            else:
                clean = enrich._clean(result, article_row, cand.story.category)
                if not clean["is_ai_news"] or word_count(clean["summary_md"]) < 60:
                    continue
            budgets = {used[2]: 10**6} if used else {}
            clean, note = enrich.verify(eng, article_row, source_text, clean, used, budgets, {},
                                        stats.setdefault("checks", {"checked": 0, "flagged": 0, "retried": 0, "edited": 0}))
            if note:
                stats.setdefault("caught", []).append(note)
            values = {"headline": clean["headline"], "summary_md": clean["summary_md"],
                      "key_points": clean["key_points"], "why_it_matters": clean["why_it_matters"],
                      "entities": clean["entities"], "importance": clean["importance"]}
            # The single-article path uses the full prompt, so it also returns the tracker and
            # work-card blocks. They are written only where the stronger model found something: an
            # answer without them must never empty what the first summary found.
            extra = {k: clean[k] for k in ("model_release", "funding", "work_card") if clean.get(k)} if not multi else {}
            notes = source_notes(result, rows, source_text) if multi else None
            with eng.begin() as conn:
                conn.execute(update(db.articles).where(db.articles.c.id == lead_row.id)
                             .values(**values, **extra, enrich_model=mark(model_used, cand.sources)))
                # The story carries its lead's digest (cluster.py). If clustering has moved the lead
                # since the candidates were picked, the article keeps the better summary and the
                # story is left alone. first_published_at and updated_at are not touched here: an
                # improved summary must never make an old story look new.
                conn.execute(update(db.stories)
                             .where(db.stories.c.id == cand.story.id, db.stories.c.lead_article_id == lead_row.id)
                             .values(**values, source_notes=notes))
            written.append(cand.story.id)
            written_articles.append(lead_row.id)
            stats["upgraded"] += 1
            stats["multi" if multi else "single"] += 1
            log.info("story #%s rewritten by %s from %d source(s)", cand.story.id, model_used, len(rows) if multi else 1)
            if model_used and model_used.startswith("gemini"):
                time.sleep(4.2)  # 15 requests per minute on the free tier
    finally:
        enrich._deadline = None
    if written:
        # The day's count of upgrades lives beside the providers' own counts, so the cap survives
        # a restart and the dashboard can show it.
        enrich.record_usage(eng, "upgrade", len(written))
        with eng.connect() as conn:
            # The rewritten rows are read again from the database next run, whatever their length.
            cache.forget_details(conn, cache.STORY_TEXT, written)
            cache.forget_details(conn, cache.ARTICLE_TEXT, written_articles)
    return stats
