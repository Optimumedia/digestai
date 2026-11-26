"""The site's test data: python tests/site_fixture.py [--out DIR] [--force]

CI builds the site from this before it deploys anything (.github/workflows/postgres-tests.yml), so a
page template that breaks fails there and not on digestai.news. It writes what the real pipeline
writes: a small made-up database (below) goes through the real export, admin and team steps into
site/src/data. Every word in it was written for this test: no publisher's text, no real company, no
real person. Dates are relative to now, so /today, the briefing and the daily pages have stories.

The fixture covers what the built-site tests and the smoke test (test_site_smoke.py) look for: a
story confirmed by three outlets with full text, a single-outlet one, a thread, a model launch with a
price, a funding round, an AI at Work card, a story merged into another (a redirect page), a spoken
briefing that names a story with no page, and a run history for the admin pages.

It refuses to overwrite a real export in site/src/data unless --force is given."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from support import SITE_DATA  # noqa: E402

MARK = "digest-site-fixture"

SOURCES = [
    # id, key, name, feed url, type
    (1, "northwind", "Northwind Labs blog", "https://northwind.test/blog/feed", "primary"),
    (2, "ledger", "The Daily Ledger", "https://ledger.test/feed", "press"),
    (3, "circuit", "Circuit Weekly", "https://circuit.test/feed", "press"),
    (4, "shopfront", "Shopfront Notes", "https://shopfront-notes.test/feed", "press"),
    (5, "briefly", "Briefly", "https://briefly.test/feed", "newsletter"),
    (6, "forum", "Builders Forum", "https://forum.test", "community"),
]
SOURCE_DOMAIN = {1: "northwind.test", 2: "ledger.test", 3: "circuit.test", 4: "shopfront-notes.test", 5: "briefly.test", 6: "forum.test"}

# The lead article of the first story has full text, as a story page shows it. Written for this test.
FULL_TEXT = """# Atlas 3 is available today

Northwind Labs is releasing Atlas 3, the third model in its Atlas family, with open weights under the Apache 2.0 licence and a context window of one million tokens.

The model can be downloaded from the company's own site from today, and a hosted version is available through the Northwind API for developers who do not want to run it themselves.

Northwind Labs says the hosted version costs one dollar per million input tokens and four dollars per million output tokens, the same as the previous Atlas model.

The company published its evaluation results alongside the weights, and says independent groups are welcome to repeat them on their own hardware.
"""

ATLAS = {"name": "Atlas 3", "lab": "Northwind Labs", "kind": "llm", "availability": "open_weights", "license": "Apache 2.0",
         "context": "1M tokens", "link": "https://northwind.test/atlas-3"}
CARD = {
    "fits": True,
    "tool": "Shopfront Ad Writer",
    "maker": "Shopfront",
    "what_it_does": "Writes three versions of a product ad from the product page.",
    "who_for": ["marketer", "small business"],
    "use_for": ["Draft ads for a new product", "Write a seasonal sale ad", "Test two headlines"],
    "cost": "free tier",
    "effort": "minutes",
    "watch_out": "The free tier allows ten ads a month.",
    "link": "https://shopfront.test/ad-writer",
}

# hours ago first published, slug, headline, category, importance, story text, articles:
# (source id, title, our headline, extra article fields)
STORIES = [
    (2, "northwind-labs-releases-atlas-3", "Northwind Labs releases Atlas 3, an open-weights model with a 1M-token context", "models", 8,
     {"summary": "Northwind Labs released Atlas 3 with open weights under the Apache 2.0 licence. Atlas 3 costs $1 per million input "
                 "tokens and $4 per million output tokens on the Northwind API. The weights can be downloaded today.",
      "points": ["Atlas 3 has open weights under Apache 2.0", "The context window is one million tokens",
                 "The hosted version costs $1 per million input tokens and $4 per million output tokens"],
      "why": "An open model with a long context gives developers a choice they can run on their own hardware.",
      "entities": {"companies": ["Northwind Labs"], "models": ["Atlas 3"], "people": []}, "thread": 1},
     [(1, "Introducing Atlas 3", "Northwind Labs releases Atlas 3 with open weights", {"content_md": FULL_TEXT, "model_release": ATLAS}),
      (2, "Northwind's Atlas 3 arrives with open weights", "Northwind Labs releases Atlas 3", {"model_release": ATLAS}),
      (3, "Atlas 3: what developers get", "Atlas 3 launches with a one million token context", {"model_release": ATLAS}),
      (6, "Atlas 3 released (discussion)", "Builders discuss the Atlas 3 release",
       {"discussion_site": "hn", "discussion_url": "https://forum.test/item/1", "discussion_points": 240})]),
    (3, "shopfront-adds-an-ai-ad-writer-for-small-shops", "Shopfront adds an AI ad writer for small online shops", "marketing", 7,
     {"summary": "Shopfront added an ad writer that drafts three versions of a product ad from the product page. It is free for "
                 "ten ads a month.",
      "points": ["The ad writer drafts three versions of an ad", "The free tier allows ten ads a month"],
      "why": "Small shops can draft ads without hiring a copywriter.",
      "entities": {"companies": ["Shopfront"], "models": [], "people": []}},
     [(4, "Shopfront's new ad writer", "Shopfront adds an AI ad writer", {"work_card": CARD}),
      (2, "Shopfront brings AI ad copy to small sellers", "Shopfront launches an ad writer for small sellers", {"work_card": CARD})]),
    (5, "quill-robotics-raises-120-million", "Quill Robotics raises $120 million to build warehouse robots", "business", 7,
     {"summary": "Quill Robotics raised $120 million in a Series B round to build robots that pick and pack orders in warehouses.",
      "points": ["The round is a Series B of $120 million", "The money pays for a second factory"],
      "why": "Warehouse robots are one of the first places AI hardware earns money.",
      "entities": {"companies": ["Quill Robotics"], "models": [], "people": []}},
     [(2, "Quill Robotics raises $120 million", "Quill Robotics raises $120 million in a Series B",
       {"funding": {"company": "Quill Robotics", "amount_usd": 1.2e8, "round": "series b"}}),
      (3, "Quill's Series B", "Quill Robotics raises $120 million for warehouse robots",
       {"funding": {"company": "Quill Robotics", "amount_usd": 1.2e8, "round": "series b"}})]),
    (7, "freedonia-publishes-draft-rules-for-ai-hiring-tools", "Freedonia publishes draft rules for AI hiring tools", "policy", 6,
     {"summary": "The government of Freedonia published draft rules that require employers to tell applicants when an AI tool "
                 "screens their application.",
      "points": ["Employers must tell applicants when AI screens them", "Comments are open for sixty days"],
      "why": "Hiring is one of the first areas where AI rules apply to ordinary employers.",
      "entities": {"companies": [], "models": [], "people": []}},
     [(2, "Freedonia drafts AI hiring rules", "Freedonia publishes draft rules for AI hiring tools", {}),
      (3, "What Freedonia's hiring rules ask of employers", "Freedonia drafts rules for AI in hiring", {})]),
    (9, "lumen-launches-an-agent-that-books-meetings", "Lumen launches an agent that books meetings from email", "agents", 6,
     {"summary": "Lumen launched an agent that reads meeting requests in email and books a time in the user's calendar.",
      "points": ["The agent reads meeting requests in email", "It asks before it accepts a meeting"],
      "why": "Scheduling is a common first job for office agents.",
      "entities": {"companies": ["Lumen"], "models": [], "people": []}},
     [(3, "Lumen's scheduling agent", "Lumen launches a scheduling agent", {})]),
    (12, "kestrel-chips-shows-a-low-power-inference-chip", "Kestrel Chips shows a low-power inference chip", "hardware", 6,
     {"summary": "Kestrel Chips showed an inference chip that it says runs small language models on two watts.",
      "points": ["The chip runs small models on two watts", "Samples ship next year"],
      "why": "Low-power chips let phones and sensors run models without a server.",
      "entities": {"companies": ["Kestrel Chips"], "models": [], "people": []}},
     [(2, "Kestrel's two-watt chip", "Kestrel Chips shows a two-watt inference chip", {}),
      (5, "Briefly: Kestrel's new chip", "Kestrel Chips shows a low-power chip", {})]),
    (20, "northwind-labs-opens-atlas-3-to-developers-in-europe", "Northwind Labs opens Atlas 3 to developers in Europe", "models", 6,
     {"summary": "Northwind Labs opened the hosted Atlas 3 to developers in Europe, a day after the release elsewhere.",
      "points": ["Atlas 3 is now hosted in Europe", "Prices are the same as elsewhere"],
      "why": "Developers in Europe can now use the hosted model without sending data abroad.",
      "entities": {"companies": ["Northwind Labs"], "models": ["Atlas 3"], "people": []}, "thread": 1},
     [(1, "Atlas 3 in Europe", "Northwind Labs opens Atlas 3 in Europe", {}),
      (2, "Atlas 3 reaches Europe", "Northwind Labs brings Atlas 3 to Europe", {})]),
    (26, "survey-finds-most-small-firms-use-ai-for-email", "Survey finds most small firms use AI for email", "society", 5,
     {"summary": "A survey of small firms found that most use an AI tool to draft email, and fewer use one for accounts.",
      "points": ["Most small firms use AI to draft email", "Fewer use it for accounts"],
      "why": "It shows where small firms find AI useful first.",
      "entities": {"companies": [], "models": [], "people": []}},
     [(4, "Small firms and AI email", "Survey finds small firms use AI for email", {}),
      (3, "What small firms use AI for", "Small firms mostly use AI for email, survey finds", {})]),
    (30, "a-method-for-shorter-model-answers", "Researchers describe a method for shorter model answers", "research", 4,
     {"summary": "Researchers described a training method that makes model answers shorter without losing accuracy on their tests.",
      "points": ["Answers were a third shorter", "Accuracy held on the tests used"],
      "why": "Shorter answers cost less to serve.",
      "entities": {"companies": [], "models": [], "people": []}},
     [(5, "Shorter answers, same accuracy", "Researchers describe a method for shorter answers", {})]),
    (40, "harbor-bank-uses-ai-to-sort-customer-letters", "Harbor Bank uses AI to sort customer letters", "enterprise", 5,
     {"summary": "Harbor Bank said it uses an AI model to sort incoming customer letters by subject before a person reads them.",
      "points": ["Letters are sorted by subject", "A person still reads every letter"],
      "why": "Sorting mail is a common first use of AI in banks.",
      "entities": {"companies": ["Harbor Bank"], "models": [], "people": []}},
     [(2, "Harbor Bank sorts letters with AI", "Harbor Bank uses AI to sort customer letters", {}),
      (3, "How Harbor Bank sorts its post", "Harbor Bank sorts customer letters with AI", {})]),
    # Northwind's earlier model: a lab needs two tracked models for the /models/latest table.
    (60, "northwind-labs-releases-atlas-2-mini-for-phones", "Northwind Labs releases Atlas 2 Mini for phones", "models", 6,
     {"summary": "Northwind Labs released Atlas 2 Mini, a small model that runs on phones without a connection.",
      "points": ["Atlas 2 Mini runs on phones", "It works without a connection"],
      "why": "Small models on phones keep data on the device.",
      "entities": {"companies": ["Northwind Labs"], "models": ["Atlas 2 Mini"], "people": []}},
     [(1, "Atlas 2 Mini", "Northwind Labs releases Atlas 2 Mini",
       {"model_release": {"name": "Atlas 2 Mini", "lab": "Northwind Labs", "kind": "llm", "availability": "consumer"}}),
      (3, "Northwind's phone model", "Northwind Labs releases Atlas 2 Mini for phones",
       {"model_release": {"name": "Atlas 2 Mini", "lab": "Northwind Labs", "kind": "llm", "availability": "consumer"}})]),
]
# The company's own picture for the first story (the images step writes this file from the web).
PRIMARY_IMAGES = {"northwind-labs-releases-atlas-3": {"url": "https://northwind.test/atlas-3.png", "width": 1600, "height": 900,
                                                      "ideal": True}}
# A duplicate of the first story, merged into it: its address becomes a redirect page.
MERGED_SLUG = "atlas-3-is-out-now"
# A story an episode names that has no page (never in the database).
GONE_SLUG = "a-story-that-was-unpublished"


def seed(eng, now) -> None:
    from sqlalchemy import insert

    from digest import db

    with eng.begin() as conn:
        conn.execute(insert(db.sources), [
            {"id": i, "key": key, "name": name, "url": url, "source_type": kind, "kind": "hn" if kind == "community" else "rss"}
            for i, key, name, url, kind in SOURCES])
        conn.execute(insert(db.threads), [{
            "id": 1, "slug": "northwind-atlas-3", "title": "Northwind Labs and Atlas 3",
            "summary": "Northwind Labs released Atlas 3 and then opened it to more developers.", "category": "models",
            "entities": {"companies": ["Northwind Labs"]}, "story_count": 2, "named_count": 2,
            "first_at": now - timedelta(hours=20), "updated_at": now - timedelta(hours=2), "status": "published"}])
        aid = 0
        for sid, (hours, slug, headline, category, importance, text, arts) in enumerate(STORIES, start=1):
            first = now - timedelta(hours=hours)
            lead = aid + 1
            conn.execute(insert(db.stories).values(
                id=sid, slug=slug, headline=headline, summary_md=text["summary"], key_points=text["points"],
                why_it_matters=text["why"], category=category, entities=text["entities"], lead_article_id=lead,
                article_count=len(arts), importance=importance, score=round(0.9 - sid * 0.05, 2), status="published",
                thread_id=text.get("thread"), first_published_at=first, updated_at=first + timedelta(minutes=40)))
            for k, (src, title, our_headline, extra) in enumerate(arts):
                aid += 1
                body = extra.get("content_md")
                conn.execute(insert(db.articles).values(
                    id=aid, url=f"https://{SOURCE_DOMAIN[src]}/{slug}-{aid}", source_id=src, story_id=sid,
                    slug=f"{slug}-{aid}", title=title, raw_title=title, headline=our_headline, author="Staff writer",
                    domain=SOURCE_DOMAIN[src], published_at=first + timedelta(minutes=10 * k), fetched_at=first, created_at=first,
                    description=f"{title}.", content_md=body, content_text=body,
                    word_count=len(body.split()) if body else 0, show_fulltext=bool(body), extraction_ok=bool(body),
                    status="published", summary_md=text["summary"], key_points=text["points"][:2], why_it_matters=text["why"],
                    category=category, entities=text["entities"], content_type="news", importance=importance,
                    enrich_model="fixture", predicted_score=0.5, engagement=0.0,
                    model_release=extra.get("model_release"), funding=extra.get("funding"), work_card=extra.get("work_card"),
                    discussion_site=extra.get("discussion_site"), discussion_url=extra.get("discussion_url"),
                    discussion_points=extra.get("discussion_points")))
        conn.execute(insert(db.stories).values(
            id=len(STORIES) + 1, slug=MERGED_SLUG, headline="Atlas 3 is out now", summary_md="Merged into the Atlas 3 story.",
            category="models", article_count=1, importance=5, score=0.1, status="merged", redirect_to=1,
            first_published_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=2)))
        # A pipeline run an hour ago and one now, for the admin pages.
        for back, step, stats in [(60, "fetch", {"articles": 40}), (59, "export", {"stories": len(STORIES)}),
                                  (1, "fetch", {"articles": 12}), (0, "export", {"stories": len(STORIES)})]:
            at = now - timedelta(minutes=back)
            conn.execute(insert(db.runs).values(started_at=at, finished_at=at + timedelta(seconds=20), step=step, stats=stats))


def episodes(now) -> list[dict]:
    """Today's spoken briefing (the audio step writes this file). It also names a story that has no page
    any more, as an old episode can (unpublished since): /listen must not link to it."""
    day = now.date().isoformat()
    named = [(slug, headline) for _h, slug, headline, *_rest in STORIES[:2]] + [(GONE_SLUG, "A story that was unpublished")]
    return [{"date": day, "title": f"AI briefing, {day}: Atlas 3 and an ad writer for small shops",
             "file": f"briefing-{day}.mp3", "url": "", "bytes": 1_200_000, "seconds": 300,
             "publishedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "description": "Today's AI news in five minutes.",
             "stories": [{"slug": s, "headline": h} for s, h in named],
             "transcript": "Northwind Labs released Atlas 3.\n\nShopfront added an ad writer for small shops."}]


def build(out: Path) -> dict:
    """Seed a temporary database and run the real export, admin and team steps into `out`."""
    import test_reads as tr

    from digest import admin, archive, config, db, export, team

    # Outside the fixture nothing is read: not the live site, not the published archive, not a model.
    os.environ.pop("GITHUB_ACTIONS", None)
    os.environ.pop("TEST_POSTGRES_URL", None)
    archive._download = lambda *a, **k: None
    team._model_call = lambda info: (_ for _ in ()).throw(RuntimeError("no model in the fixture"))
    out.mkdir(parents=True, exist_ok=True)
    with tr.fresh_db() as (eng, _tmp):
        now = db.utcnow()
        seed(eng, now)
        config.SITE_DATA_DIR = out
        stats = {"export": export.run()}
        stats["admin"] = bool(admin.run())
        stats["team"] = team.run()
    (out / "primary-images.json").write_text(json.dumps(PRIMARY_IMAGES), encoding="utf-8")
    (out / "episodes.json").write_text(json.dumps(episodes(now)), encoding="utf-8")
    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["fixture"] = MARK
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return stats


def is_fixture(out: Path) -> bool:
    try:
        return json.loads((out / "meta.json").read_text(encoding="utf-8")).get("fixture") == MARK
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the site's test data")
    ap.add_argument("--out", type=Path, default=SITE_DATA)
    ap.add_argument("--force", action="store_true", help="overwrite a real export")
    args = ap.parse_args(argv)
    out = args.out.resolve()
    if (out / "stories.json").exists() and not is_fixture(out) and not args.force:
        print(f"{out} holds a real export; pass --force to replace it with the fixture")
        return 2
    # Files from an earlier export (episodes, topics, search data) would mix real data into the fixture.
    for old in out.glob("*.json"):
        old.unlink()
    stats = build(out)
    ex = stats["export"]
    print(f"fixture written to {out}: {ex['stories']} stories, {ex['threads']} thread, {ex['models']} models, "
          f"{ex['funding']} funding, {ex['work']} work cards, {ex['redirects']} redirects, briefing {ex['briefing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
