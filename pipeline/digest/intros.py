"""Step: model-written introductions for the recap and tracker pages.

Each weekly recap (/week/<key>) and the two trackers (/models, /funding) get a short
paragraph that summarises what the page holds, so the page carries unique prose for
readers and search engines. Stored in the topics table (kind "page") and exported in
topics.json alongside topic descriptions. Budgeted like topic intros.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update

from . import config, db, enrich

log = logging.getLogger("digest.intros")

MAX_PER_RUN = 3
MIN_WEEK_STORIES = 5
WEEK_PROMPT = """You write the opening paragraph of a weekly recap page on an AI news site.
Week: {range}
The most important stories of that week, in order:
{headlines}
Model releases that week: {models}
Funding rounds that week: {deals}

Return ONLY JSON: {{"description": "<3-4 sentences, 60-90 words: what defined the week in AI, naming the two or three biggest developments and the thread connecting them. Neutral, factual, past tense, no hype, no 'this page', no bullet points>"}}"""
TRACKER_PROMPT = """You write the opening paragraph of a data page on an AI news site.
Page: {page}
Rows (newest first):
{rows}

Return ONLY JSON: {{"description": "<3 sentences, 50-80 words: what the table shows overall and the two or three most notable recent entries. Neutral, factual, present tense, no hype, no 'this page'>"}}"""


def week_key(iso: str | None) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else datetime.now(timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_range(key: str) -> str:
    y, w = key.split("-W")
    monday = datetime.fromisocalendar(int(y), int(w), 1)
    sunday = monday + timedelta(days=6)
    return f"{monday.day} {monday:%b} to {sunday.day} {sunday:%B %Y}"


def _jobs(stories: list[dict], trackers: dict) -> list[dict]:
    """Every page that can take an intro, with the count that decides refreshes."""
    weeks: dict[str, list[dict]] = {}
    for s in stories:
        weeks.setdefault(week_key(s.get("firstPublishedAt") or s.get("updatedAt")), []).append(s)
    jobs = []
    for key, lst in sorted(weeks.items(), reverse=True):
        if len(lst) < MIN_WEEK_STORIES:
            continue
        lst.sort(key=lambda s: (s.get("importance", 0), s.get("score", 0)), reverse=True)
        models = [m["name"] for m in trackers.get("models", []) if week_key(m.get("date")) == key]
        deals = [f"{f['company']} ({f.get('round') or 'round'})" for f in trackers.get("funding", []) if week_key(f.get("date")) == key]
        jobs.append({
            "slug": f"week-{key.lower()}", "name": f"Week {key}", "count": len(lst),
            "prompt": WEEK_PROMPT.format(range=week_range(key), headlines="\n".join(f"- {s['headline']}" for s in lst[:10]),
                                         models=", ".join(models[:8]) or "none recorded", deals=", ".join(deals[:8]) or "none recorded"),
        })
    models = trackers.get("models", [])
    if len(models) >= 5:
        rows = "\n".join(
            f"- {m['name']} by {m.get('lab') or 'unknown lab'}, {m.get('availability') or ''}{(' (' + m['license'] + ')') if m.get('license') else ''}, {m.get('date') or ''}"
            for m in models[:15])
        jobs.append({"slug": "page-models", "name": "AI model release tracker", "count": len(models),
                     "prompt": TRACKER_PROMPT.format(page="AI model release tracker: every model launch covered, with lab, availability and license", rows=rows)})
    funding = trackers.get("funding", [])
    if len(funding) >= 5:
        rows = "\n".join(f"- {f['company']}: ${(f.get('amount_usd') or 0) / 1e6:.0f}M {f.get('round') or ''}, {f.get('date') or ''}" for f in funding[:15])
        jobs.append({"slug": "page-funding", "name": "AI funding tracker", "count": len(funding),
                     "prompt": TRACKER_PROMPT.format(page="AI funding and deals tracker: rounds, acquisitions and valuations as reported", rows=rows)})
    return jobs


def run() -> dict:
    stats = {"written": 0, "pages": 0}
    stories_file = config.SITE_DATA_DIR / "stories.json"
    trackers_file = config.SITE_DATA_DIR / "trackers.json"
    if not stories_file.exists():
        return stats
    stories = json.loads(stories_file.read_text(encoding="utf-8"))
    trackers = json.loads(trackers_file.read_text(encoding="utf-8")) if trackers_file.exists() else {}
    jobs = _jobs(stories, trackers)
    stats["pages"] = len(jobs)
    if not jobs:
        return stats

    eng = db.engine()
    now = db.utcnow()
    with eng.begin() as conn:
        existing = {t.slug: t for t in conn.execute(select(db.topics).where(db.topics.c.kind == "page")).all()}
        for j in jobs:
            if j["slug"] in existing:
                conn.execute(update(db.topics).where(db.topics.c.slug == j["slug"]).values(story_count=j["count"], updated_at=now))
            else:
                conn.execute(insert(db.topics).values(slug=j["slug"], name=j["name"], kind="page", story_count=j["count"], described_at_count=0, updated_at=now))
        existing = {t.slug: t for t in conn.execute(select(db.topics).where(db.topics.c.kind == "page")).all()}

    provider = "groq" if config.GROQ_API_KEY else "gemini" if config.GEMINI_API_KEY else None
    if not provider:
        return _export(eng, stats)
    with eng.connect() as conn:
        budget = min(MAX_PER_RUN, enrich.allowance(conn, provider))
    this_week = f"week-{week_key(None).lower()}"
    for j in jobs:
        if stats["written"] >= budget:
            break
        t = existing[j["slug"]]
        if t.description:
            # Past weeks are settled once written; the current week and the trackers refresh
            # when they have grown by a third since the last write.
            if not (j["slug"] == this_week or j["slug"].startswith("page-")):
                continue
            if j["count"] < 1.34 * (t.described_at_count or 1):
                continue
        try:
            result = enrich.call_groq(j["prompt"]) if provider == "groq" else enrich.call_gemini(j["prompt"])
            enrich.record_usage(eng, provider, 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("intro failed for %s: %s", j["slug"], exc)
            continue
        desc = str(result.get("description") or "").strip()[:800]
        if len(desc) < 50:
            continue
        with eng.begin() as conn:
            conn.execute(update(db.topics).where(db.topics.c.id == t.id).values(description=desc, described_at_count=j["count"], updated_at=now))
        stats["written"] += 1
    return _export(eng, stats)


def _export(eng, stats: dict) -> dict:
    """Rewrite topics.json with every described row (topics and pages)."""
    with eng.connect() as conn:
        rows = conn.execute(select(db.topics).where(db.topics.c.description.isnot(None))).all()
    (config.SITE_DATA_DIR / "topics.json").write_text(
        json.dumps({r.slug: {"name": r.name, "kind": r.kind, "description": r.description} for r in rows}, ensure_ascii=False), encoding="utf-8")
    return stats
