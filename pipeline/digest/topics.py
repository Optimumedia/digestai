"""Step: give topic hub pages (companies, models, people) a model-written description so each
hub carries unique text for search engines and readers, not just a list of headlines.

The model sees only what our own stories say about the topic (headline and digest) and is told to
write from that alone. Every capitalised name in its answer is then checked against that text; an
answer that names an organisation, model or person the stories never mention is dropped for a
plain templated line ("N stories since <date>, most recently ..."), because earlier intros written
from the model's memory stated invented facts as if they were ours.

Budgeted: a few descriptions per run, refreshed when a topic's story count has doubled.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import insert, select, update

from . import cache, config, db, enrich
from .textutil import slugify

log = logging.getLogger("digest.topics")

MAX_PER_RUN = 5
MIN_STORIES = 3
MAX_STORIES_IN_PROMPT = 12
SUMMARY_CHARS = 240
PROMPT = """You write short introductions for topic pages on an AI news site.
Topic: {name} ({kind})
What the site's own stories say about it, newest first (headline, then the digest):
{stories}

Write only from the text above. Do not add anything you know from elsewhere: no founder, owner, headquarters, product, date or figure that the stories do not state. If the stories do not say what {name} is, do not guess; say what the coverage is about instead. Name only organisations, models and people that appear above.

Return ONLY JSON: {{"description": "<2-3 sentences, 40-70 words: what {name} is according to the stories, and what the recent news is about. Neutral, factual, no hype, no 'this page', present tense>"}}"""

# Capitalised words that are not names: sentence starters, months, pronouns, the site's own name.
_COMMON = set("""a an the this that these those it its in on at as of and or but for with from by to
after before since while when where which who whose what how if then than also both other some many
most much more several new recent recently now last first second third one two three meanwhile
however still yet here there we they he she i you our their his her my your ai api ceo cto llm llms
gpu gpus us uk eu january february march april may june july august september october november
december monday tuesday wednesday thursday friday saturday sunday digest""".split())
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.'’&+-]*")


def _words(text: str) -> set[str]:
    """Lowercased tokens with possessives and trailing punctuation removed."""
    out = set()
    for tok in _TOKEN.findall(text):
        tok = re.sub(r"(?:'s|’s)$", "", tok).strip(".'’&+-").lower()
        if tok:
            out.add(tok)
    return out


def unsupported_names(description: str, source_text: str) -> list[str]:
    """Capitalised words in the description that the source text never uses, in order of appearance.
    Digits count as capitals ("GPT-6"), so invented model numbers are caught too. Common words
    that happen to start a sentence are ignored; anything else must appear somewhere in the
    stories, in any case."""
    known = _words(source_text)
    bad: list[str] = []
    for tok in _TOKEN.findall(description):
        if not (tok[0].isupper() or tok[0].isdigit()):
            continue
        word = re.sub(r"(?:'s|’s)$", "", tok).strip(".'’&+-")
        if not word or word.lower() in _COMMON or word.lower() in known:
            continue
        if word.isdigit() and len(word) <= 2:
            continue
        if word not in bad:
            bad.append(word)
    return bad


def _first_paragraph(md: str | None, limit: int = SUMMARY_CHARS) -> str:
    text = re.sub(r"[#*_`>]", "", (md or "").split("\n\n")[0]).strip()
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _newest_first(story_list: list[dict]) -> list[dict]:
    return sorted(story_list, key=lambda s: s.get("firstPublishedAt") or s.get("updatedAt") or "", reverse=True)


def story_lines(story_list: list[dict]) -> str:
    """The prompt's evidence block: one headline and digest opening per story, newest first."""
    lines = []
    for s in _newest_first(story_list)[:MAX_STORIES_IN_PROMPT]:
        digest = _first_paragraph(s.get("summaryMd")) or " ".join(s.get("keyPoints") or [])[:SUMMARY_CHARS]
        lines.append(f"- {s['headline']}" + (f"\n  {digest}" if digest else ""))
    return "\n".join(lines)


def _day(iso: str | None) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {d:%B %Y}"


def templated_intro(story_list: list[dict]) -> str:
    """What the hub can say without a model: how much coverage there is, since when, and the
    latest headline. The site recognises the "N stories since" opening and does not offer it as
    the answer to "What is X?"."""
    ordered = _newest_first(story_list)
    n = len(ordered)
    first = _day(ordered[-1].get("firstPublishedAt") or ordered[-1].get("updatedAt")) if ordered else ""
    latest = ordered[0]["headline"] if ordered else ""
    return f"{n} {'story' if n == 1 else 'stories'} since {first}, most recently: {latest}."


def grounded_description(name: str, kind: str, story_list: list[dict], ask) -> tuple[str, bool]:
    """(description, written by the model?). `ask(prompt)` returns the model's JSON dict; on any
    failure or an ungrounded answer the templated line is returned instead."""
    evidence = story_lines(story_list)
    prompt = PROMPT.format(name=name, kind=kind, stories=evidence)
    try:
        desc = str(ask(prompt).get("description") or "").strip()[:600]
    except Exception as exc:  # noqa: BLE001
        log.warning("topic description failed for %s: %s", name, exc)
        return templated_intro(story_list), False
    bad = unsupported_names(desc, f"{name}\n{evidence}") if len(desc) >= 40 else ["(too short)"]
    if bad:
        log.info("topic intro for %s not grounded (%s); using the templated line", name, ", ".join(bad[:5]))
        return templated_intro(story_list), False
    return desc, True


def run() -> dict:
    stats = {"described": 0, "templated": 0, "topics": 0}
    entities_file = config.SITE_DATA_DIR / "entities.json"
    stories_file = config.SITE_DATA_DIR / "stories.json"
    if not entities_file.exists() or not stories_file.exists():
        return stats
    entities = json.loads(entities_file.read_text(encoding="utf-8"))
    stories = {s["id"]: s for s in json.loads(stories_file.read_text(encoding="utf-8"))}
    eng = db.engine()
    now = db.utcnow()

    # Sync the topic table with what the export knows (merged by slug).
    merged: dict[str, dict] = {}
    for e in entities:
        slug = slugify(e["name"])
        if not slug:
            continue
        m = merged.setdefault(slug, {"name": e["name"], "kind": e["kind"], "ids": set()})
        m["ids"].update(e["storyIds"])
    with eng.begin() as conn:
        existing = {t.slug for t in conn.execute(select(db.topics.c.slug)).all()}
        for slug, m in merged.items():
            if slug in existing:
                conn.execute(update(db.topics).where(db.topics.c.slug == slug).values(story_count=len(m["ids"]), updated_at=now))
            else:
                conn.execute(insert(db.topics).values(slug=slug, name=m["name"], kind=m["kind"], story_count=len(m["ids"]), described_at_count=0, updated_at=now))
    stats["topics"] = len(merged)

    if not (config.GROQ_API_KEY or config.GEMINI_API_KEY):
        return stats
    provider = "groq" if config.GROQ_API_KEY else "gemini"
    ask = enrich.call_groq if provider == "groq" else enrich.call_gemini
    with eng.connect() as conn:
        due = conn.execute(
            select(db.topics.c.id, db.topics.c.slug, db.topics.c.name, db.topics.c.kind, db.topics.c.story_count,
                   db.topics.c.described_at_count)
            .where(db.topics.c.story_count >= MIN_STORIES, db.topics.c.kind != "page")
            .order_by(db.topics.c.story_count.desc())
        ).all()
        allowance = enrich.allowance(conn, provider)
    budget = min(MAX_PER_RUN, allowance)
    for t in due:
        if stats["described"] + stats["templated"] >= budget:
            break
        # Describe once at 3 stories, refresh when the count has doubled since (a templated line
        # counts as described, so the model is not asked again until the hub has grown).
        if t.described_at_count and t.story_count < 2 * t.described_at_count:
            continue
        ids = merged.get(t.slug, {}).get("ids", set())
        story_list = [stories[i] for i in ids if i in stories]
        if len(story_list) < MIN_STORIES:
            continue
        kind = {"companies": "company", "models": "AI model", "people": "person"}.get(t.kind, "topic")
        desc, by_model = grounded_description(t.name, kind, story_list, ask)
        enrich.record_usage(eng, provider, 1)
        with eng.begin() as conn:
            conn.execute(update(db.topics).where(db.topics.c.id == t.id).values(description=desc, described_at_count=t.story_count, updated_at=now))
        stats["described" if by_model else "templated"] += 1

    with eng.connect() as conn:
        rows = cache.described_topics(conn)
    (config.SITE_DATA_DIR / "topics.json").write_text(
        json.dumps({r.slug: {"name": r.name, "kind": r.kind, "description": r.description} for r in rows}, ensure_ascii=False), encoding="utf-8")
    return stats
