"""AI at Work (/work): card validation, the tool directory, the section briefing and the data the
section's pages are built from. Offline: python tests/test_work.py"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import update  # noqa: E402

from digest import db, work  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def card(**over) -> dict:
    base = {
        "fits": True,
        "tool": "Canva Magic Studio",
        "maker": "Canva",
        "what_it_does": "Writes and lays out social posts from a short brief.",
        "who_for": ["marketer"],
        "use_for": ["Draft a week of posts", "Resize one ad for five places", "Write product captions"],
        "cost": "free tier",
        "effort": "minutes",
        "watch_out": "The free tier watermarks video exports.",
        "link": "https://www.canva.com/magic-studio/",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------- card validation

def test_a_card_is_kept_only_when_it_can_keep_its_promises():
    assert work.clean_card(card()) is not None
    assert work.clean_card(None) is None
    assert work.clean_card("yes") is None
    assert work.clean_card(card(fits=False)) is None
    assert work.clean_card(card(tool="")) is None, "no tool, no card"
    assert work.clean_card(card(watch_out="")) is None, "every card owes the reader a caveat"
    assert work.clean_card(card(watch_out="None.")) is None, "'None.' is not a caveat"
    assert work.clean_card(card(what_it_does="It helps.")) is None, "one vague clause is not a sentence"
    assert work.clean_card(card(use_for=[])) is None


def test_a_card_is_clamped_to_what_the_pages_can_show():
    out = work.clean_card(card(
        who_for=["marketer", "marketer", "ceo", "sales", "support", "ops", "founder"],
        use_for=["A " * 80, "Second use", "Third use", "Fourth use"],
        tool="T" * 300, watch_out="W" * 400, what_it_does="D" * 400, link="ftp://example.com/x"))
    assert out["who_for"] == ["marketer", "sales", "support", "ops"], "unknown readers dropped, order kept, max four"
    assert len(out["use_for"]) == 3 and len(out["use_for"][0]) <= 90
    assert len(out["tool"]) == 120 and len(out["watch_out"]) == 260 and len(out["what_it_does"]) == 220
    assert out["link"] is None, "only http(s) links reach a page"
    assert work.clean_card(card(who_for=["nobody"]))["who_for"] == ["marketer"]


def test_effort_and_cost_are_read_for_their_meaning():
    assert work.clean_card(card(effort="a few minutes"))["effort"] == "minutes"
    assert work.clean_card(card(effort="You need an engineer to wire up the API"))["effort"] == "needs a developer"
    assert work.clean_card(card(effort="about half a day"))["effort"] == "an afternoon"
    assert work.clean_card(card(effort="depends"))["effort"] is None
    assert work.cost_kind("paid from $20 a month") == "paid"
    assert work.cost_kind("Free tier, then $9/mo") == "free tier"
    assert work.cost_kind("free") == "free"
    assert work.cost_kind("included in a tool you already have") == "included"
    assert work.cost_kind("") == "unknown"


def test_jobs_come_from_who_it_is_for_and_what_it_does():
    assert work.jobs_for(work.clean_card(card(who_for=["sales", "ecommerce"], use_for=["Follow up on leads"],
                                              what_it_does="Scores leads in the CRM and suggests who to call."))) == ["sell"]
    assert work.jobs_for(work.clean_card(card(who_for=["sales"], use_for=["Reply to enquiries"],
                                              what_it_does="Drafts the reply to a sales enquiry."))) == ["content", "sell"]
    assert work.jobs_for(work.clean_card(card())) == ["customers", "content"], "writing posts is making content"
    assert work.jobs_for(work.clean_card(card(who_for=["ops"], what_it_does="Reconciles invoices with the bank feed.",
                                              use_for=["Match payments"]))) == ["business"]
    assert set(work.JOBS) == {"customers", "content", "sell", "support", "business"}


def test_what_to_skip_this_week_is_stated_honestly():
    assert work.skip_reason(work.clean_card(card())) is None
    assert work.skip_reason(work.clean_card(card(effort="needs a developer"))) == "needs a developer"
    assert work.skip_reason(work.clean_card(card(watch_out="It is behind a waitlist for now."))) == "waitlist only"
    assert work.skip_reason(work.clean_card(card(watch_out="Only on enterprise plans."))) == "enterprise plans only"
    assert work.skip_reason(work.clean_card(card(watch_out="It is in beta and changes weekly."))) == "not open to everyone yet"


def test_usefulness_ranks_what_a_small_team_can_do_not_how_big_the_news_is():
    easy = work.clean_card(card())
    hard = work.clean_card(card(cost="paid from $500 a month", effort="needs a developer", link=None, maker=None,
                                use_for=["Build a custom pipeline"]))
    assert work.usefulness(easy) > work.usefulness(hard)
    # The big story is the hard one; usefulness does not care.
    big = {"importance": 10, "score": 9.0, "articleCount": 12, "contentType": "news"}
    small = {"importance": 2, "score": 0.1, "articleCount": 1, "contentType": "tutorial"}
    assert work.usefulness(easy, small) > work.usefulness(hard, big)


# ---------------------------------------------------------------------------- stories and tools

def story(sid: int, cards: dict[int, dict], **over) -> dict:
    s = {
        "id": sid, "slug": f"story-{sid}", "headline": f"Story {sid}", "leadArticleId": min(cards) if cards else None,
        "importance": 5, "score": 1.0, "articleCount": len(cards) or 1, "contentType": "news",
        "firstPublishedAt": (NOW - timedelta(hours=sid)).isoformat().replace("+00:00", "Z"),
        "updatedAt": (NOW - timedelta(hours=sid)).isoformat().replace("+00:00", "Z"),
        "articles": [{"id": aid} for aid in sorted(cards)],
    }
    s.update(over)
    return s


def with_card(sid: int, cards: dict[int, dict], **over) -> dict:
    s = story(sid, cards, **over)
    chosen = work.story_card(s, cards)
    s["workCard"] = work.card_out(chosen, s) if chosen else None
    return s


def test_a_story_takes_its_lead_articles_card_unless_another_is_fuller():
    thin = work.clean_card(card(maker=None, link=None, effort=None, use_for=["One use"]))
    full = work.clean_card(card(tool="Fuller spelling"))
    s = story(1, {10: thin, 11: full}, leadArticleId=10)
    assert work.story_card(s, {10: thin, 11: full})["tool"] == "Fuller spelling"
    same = work.clean_card(card(tool="Lead card"))
    assert work.story_card(s, {10: same, 11: full})["tool"] == "Lead card", "a lead that is as good wins"
    assert work.story_card(story(2, {}), {}) is None


def test_the_tool_directory_keeps_one_row_per_tool_newest_change_first():
    stories = [
        with_card(1, {1: work.clean_card(card(tool="Magic Studio", maker="Canva Inc.", link=None, effort=None))}),
        with_card(3, {3: work.clean_card(card(tool="Canva Magic Studio", maker="Canva"))}),
        with_card(2, {2: work.clean_card(card(tool="Zapier Agents", maker="Zapier", who_for=["ops"],
                                              what_it_does="Runs a saved workflow when a form is filled in.",
                                              use_for=["Route new leads"], cost="paid from $20 a month"))}),
    ]
    rows = work.build_tools(stories)
    assert len(rows) == 2, [r["tool"] for r in rows]
    canva = next(r for r in rows if "Canva" in r["tool"])
    assert canva["tool"] == "Canva Magic Studio", "the fuller spelling is the one the directory shows"
    assert canva["maker"] == "Canva Inc.", "the newest card's own words win where it has them"
    assert canva["link"] and canva["effort"] == "minutes", "what the newest card left out is filled in from an older one"
    assert canva["changeCount"] == 2 and [c["slug"] for c in canva["changes"]] == ["story-1", "story-3"]
    assert [r["lastChange"] for r in rows] == sorted((r["lastChange"] for r in rows), reverse=True)
    assert canva["firstSeen"] < canva["lastChange"]
    zapier = next(r for r in rows if r["tool"] == "Zapier Agents")
    assert zapier["costKind"] == "paid" and zapier["jobs"] == ["business"]


def test_the_section_is_broader_than_the_marketing_category():
    marketing_news = with_card(1, {}, category="marketing")  # a category story with nothing to act on
    agent_tool = with_card(2, {2: work.clean_card(card())}, category="agents")
    section = work.section_stories([marketing_news, agent_tool])
    assert [s["id"] for s in section] == [2]


# ---------------------------------------------------------------------------- briefing and weeks

def test_the_section_briefing_orders_by_usefulness_not_importance():
    big = with_card(1, {1: work.clean_card(card(tool="Big Launch", cost="paid from $500 a month",
                                                effort="needs a developer", link=None, maker=None,
                                                use_for=["Build a custom pipeline"]))}, importance=10, articleCount=9)
    small = with_card(2, {2: work.clean_card(card(tool="Small Free Thing"))}, importance=2, articleCount=1)
    plain_news = with_card(3, {})
    out = work.build_briefing([big, small, plain_news], NOW)
    assert out["storyIds"] == [2, 1], "the free five-minute thing leads"
    assert out["stats"]["items"] == 2 and out["stats"]["tools"] == 2 and out["stats"]["free"] == 1
    assert out["windowHours"] == 24 and out["date"] == "2026-09-17"


def test_the_section_briefing_widens_its_window_on_a_quiet_day_and_holds_at_five():
    old = [with_card(i, {i: work.clean_card(card(tool=f"Tool {i}"))},
                     firstPublishedAt=(NOW - timedelta(hours=30 + i)).isoformat().replace("+00:00", "Z"))
           for i in range(1, 9)]
    out = work.build_briefing(old, NOW)
    assert out["windowHours"] == 48 and len(out["storyIds"]) == 5 and len(out["alsoIds"]) == 3


def test_the_main_briefing_is_untouched_by_the_section():
    from digest import export

    stories = [with_card(i, {i: work.clean_card(card(tool=f"Tool {i}"))}, summaryMd="Words. " * 40,
                         pinned=False, category="marketing") for i in range(1, 9)]
    for s in stories:
        s["pinned"] = False
    main = export.build_briefing(stories, NOW)
    section = work.build_briefing(stories, NOW)
    assert main["storyIds"] and section["storyIds"]
    # Both may carry the same item: the front page says what happened, the section what to do.
    assert set(main["storyIds"]) & set(section["storyIds"])
    assert "windowHours" in main and main["stats"]["stories"] == len(stories)


def test_the_weekly_playbook_splits_what_to_try_from_what_to_skip():
    this_week = NOW  # Thursday of 2026-W38
    last_week = NOW - timedelta(days=8)
    def at(dt):
        return dt.isoformat().replace("+00:00", "Z")

    stories = [
        with_card(1, {1: work.clean_card(card(tool="Try Me"))}, firstPublishedAt=at(this_week)),
        with_card(2, {2: work.clean_card(card(tool="Waitlist Thing", watch_out="Behind a waitlist in most countries."))},
                  firstPublishedAt=at(this_week - timedelta(days=1))),
        with_card(3, {3: work.clean_card(card(tool="Old Thing"))}, firstPublishedAt=at(last_week)),
    ]
    out = work.weeks(stories)
    assert set(out) == {work.week_key(at(this_week)), work.week_key(at(last_week))}
    now_week = out[work.week_key(at(this_week))]
    assert now_week["changed"] == [1, 2] and now_week["try"] == [1] and now_week["skip"] == [2]
    assert now_week["tools"] == 2
    assert work.week_key("2026-09-17T12:00:00Z") == "2026-W38"


# ---------------------------------------------------------------------------- what the export writes

def test_the_export_writes_the_section_files_and_rides_the_existing_reads():
    import test_reads as tr
    from digest import cache, config, export

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng, stories=6, per_story=2)
        stored = work.clean_card(card())
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id.in_([1, 3])).values(work_card=stored))
            conn.execute(update(db.articles).where(db.articles.c.id == 5).values(
                work_card=work.clean_card(card(tool="Zapier Agents", maker="Zapier", who_for=["ops"],
                                               what_it_does="Runs a saved workflow when a form is filled in.",
                                               use_for=["Route new leads"], cost="paid from $20 a month",
                                               effort="needs a developer"))))
            conn.execute(update(db.articles).where(db.articles.c.id == 7).values(work_card={"fits": True, "tool": "Half"}))
        config.SITE_DATA_DIR = tmp / "site"
        stats = export.run()
        site = config.SITE_DATA_DIR
        stories = json.loads((site / "stories.json").read_text(encoding="utf-8"))
        work_json = json.loads((site / "work.json").read_text(encoding="utf-8"))
        brief = json.loads((site / "work-briefing.json").read_text(encoding="utf-8"))

    carded = [s for s in stories if s.get("workCard")]
    assert len(carded) == len(work_json["storyIds"]) == stats["work"] == 3
    assert not any(a["workCard"] for s in stories for a in s["articles"] if a["id"] == 7), "a half card never ships"
    one = next(s for s in carded if s["workCard"]["tool"] == "Canva Magic Studio")
    assert one["workCard"]["jobs"] and one["workCard"]["costKind"] == "free tier" and one["workCard"]["skip"] is None
    assert stats["workTools"] == len(work_json["tools"]) == 2
    assert set(work_json["jobs"]) == set(work.JOBS)
    assert work_json["weeks"], "the playbook pages need at least one week"
    assert brief["storyIds"] and len(brief["storyIds"]) <= work.BRIEFING_SIZE
    # The section rides the reads the export already does: no new mirror or detail store.
    assert "work_card" in [c.name for c in cache.ARTICLE_TEXT_COLUMNS]
    assert cache.ARTICLE_TEXT.names.count("work_card") == 1


# ---------------------------------------------------------------------------- the weekly episode

@contextmanager
def audio_sandbox():
    """The audio step's directories in a temporary tree, with a voice that costs nothing."""
    from digest import audio, config, media

    tmp = Path(tempfile.mkdtemp())
    saved = (config.CACHE_DIR, config.SITE_DATA_DIR, media.SITE_PUBLIC, audio.AUDIO_DIR, audio.WORK_DIR,
             audio._open_engine, audio._encode, audio.engine_name)
    config.CACHE_DIR, config.SITE_DATA_DIR = tmp / "cache", tmp / "site"
    media.SITE_PUBLIC, audio.AUDIO_DIR, audio.WORK_DIR = tmp / "public", tmp / "public" / "audio", tmp / "cache" / "audio"
    media.reset()
    audio._open_engine = lambda: type("E", (), {"rate": 24000})()
    audio._encode = lambda _engine, para: (b"\x00" * 128, len(para) / 15)
    audio.engine_name = lambda: "kokoro"
    try:
        yield tmp
    finally:
        (config.CACHE_DIR, config.SITE_DATA_DIR, media.SITE_PUBLIC, audio.AUDIO_DIR, audio.WORK_DIR,
         audio._open_engine, audio._encode, audio.engine_name) = saved
        media.reset()
        shutil.rmtree(tmp, ignore_errors=True)


def _section_site(site: Path, items: int = 4) -> None:
    """What the export leaves behind: the section's stories and its work.json, nothing else."""
    site.mkdir(parents=True, exist_ok=True)
    stories, ids = [], list(range(1, items + 1))
    for i in ids:
        c = work.card_out(work.clean_card(card(tool=f"Tool {i}", maker=f"Maker {i}")))
        stories.append({"id": i, "slug": f"tool-{i}", "headline": f"Tool {i} ships",
                        "firstPublishedAt": "2026-09-15T09:00:00Z", "workCard": c})
    (site / "stories.json").write_text(json.dumps(stories), encoding="utf-8")
    (site / "work.json").write_text(json.dumps(
        {"weeks": {"2026-W38": {"changed": ids, "try": ids, "skip": [], "tools": items}}}), encoding="utf-8")


def test_the_section_episode_is_weekly_waits_its_turn_and_reads_no_database():
    from digest import audio, config, db

    monday = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)   # the Monday after 2026-W38
    with audio_sandbox() as tmp:
        _section_site(config.SITE_DATA_DIR)
        budget = config.AUDIO_TIME_BUDGET_SECONDS

        # Not before the chosen hour of the chosen day.
        assert audio._run_work(monday.replace(hour=0), budget)["reason"] == "before Monday 5:00 UTC"
        # The daily briefing has first call on the run's budget; the section waits for the next run.
        thin_budget = config.WORK_AUDIO_MIN_BUDGET_SECONDS - 1
        late = audio._run_work(monday, thin_budget)
        assert late["reason"] == "no time left this run" and late["generated"] == 0
        # A Monday that could not be finished is caught up later in the week, on the same week.
        assert audio.last_week(monday + timedelta(days=3)) == "2026-W38"
        assert audio._run_work(monday + timedelta(days=3), thin_budget)["reason"] == "no time left this run"

        before = db.bytes_read()
        made = audio._run_work(monday, budget)
        assert db.bytes_read() == before, "the episode is built from what the export already wrote"
        assert made["generated"] == 1 and made["week"] == "2026-W38" and made["items"] == 4
        episode = json.loads((config.SITE_DATA_DIR / "work-episodes.json").read_text())[0]
        assert episode["week"] == "2026-W38" and episode["file"] == "work-2026-W38.mp3"
        assert episode["date"] == "2026-09-21", "dated the Monday it comes out"
        assert episode["title"].startswith("AI at Work, 14-20 September 2026: Tool ")
        assert (audio.AUDIO_DIR / "work-2026-W38.mp3").exists()
        assert len(episode["transcript"].split("\n\n")) == 6   # the opening, four items, the close
        assert not (tmp / "cache" / "audio" / "work-2026-W38").exists(), "finished parts are cleared"

        # It is never read twice, and the daily briefing's own manifest is not touched.
        assert audio._run_work(monday, budget)["reason"] == "episode exists"
        assert not (config.SITE_DATA_DIR / "episodes.json").exists()


def test_a_thin_week_gets_no_episode():
    from digest import audio, config

    monday = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    with audio_sandbox():
        _section_site(config.SITE_DATA_DIR, items=config.WORK_AUDIO_MIN_ITEMS - 1)
        thin = audio._run_work(monday, config.AUDIO_TIME_BUDGET_SECONDS)
        assert thin["reason"] == "week too thin" and thin["generated"] == 0
        assert json.loads((config.SITE_DATA_DIR / "work-episodes.json").read_text()) == []


def test_the_run_gives_the_briefing_the_budget_first():
    """Both episodes come out of one AUDIO_TIME_BUDGET_SECONDS, spent in that order."""
    from digest import audio, config

    with audio_sandbox():
        _section_site(config.SITE_DATA_DIR)
        seen = []
        saved = audio._run_briefing, audio._run_work
        audio._run_briefing = lambda now, budget: seen.append(("briefing", round(budget))) or {"generated": 0}
        audio._run_work = lambda now, budget: seen.append(("work", round(budget))) or {"generated": 0}
        try:
            stats = audio.run()
        finally:
            audio._run_briefing, audio._run_work = saved
    assert [k for k, _ in seen] == ["briefing", "work"]
    assert seen[0][1] == config.AUDIO_TIME_BUDGET_SECONDS
    assert seen[1][1] <= config.AUDIO_TIME_BUDGET_SECONDS, "the section only gets what is left"
    assert "work" in stats


# ---------------------------------------------------------------------------- how the site is wired

def test_the_section_episode_has_its_own_feed_and_leaves_the_news_show_alone():
    site = Path(__file__).resolve().parents[2] / "site"
    work_feed = (site / "src" / "pages" / "work" / "podcast.xml.ts").read_text(encoding="utf-8")
    news_feed = (site / "src" / "pages" / "podcast.xml.ts").read_text(encoding="utf-8")
    # Two feeds, two shows: neither reads the other's episodes.
    assert "workEpisodes" in work_feed and "/work/podcast.xml" in work_feed
    assert "workEpisodes" not in news_feed and "episodes" in news_feed
    assert "digestai-work-" in work_feed, "its own guids, so no app sees one show's episode twice"
    assert 'itunes:category text="Business"' in work_feed and 'itunes:category text="News"' in news_feed
    # The player, the listing and the playbook link.
    assert "latestWorkEpisode" in (site / "src" / "pages" / "work" / "index.astro").read_text(encoding="utf-8")
    assert "workEpisodes" in (site / "src" / "pages" / "listen.astro").read_text(encoding="utf-8")
    assert "workEpisodeFor" in (site / "src" / "pages" / "work" / "week" / "[week].astro").read_text(encoding="utf-8")
    assert 'readJson<Episode[]>("work-episodes.json"' in (site / "src" / "lib" / "work.ts").read_text(encoding="utf-8")


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, type(exc).__name__, exc)
    sys.exit(1 if failures else 0)
