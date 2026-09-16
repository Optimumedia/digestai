"""Offline tests for the content quality monitor and the admin action cards:
python tests/test_quality.py (or pytest)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import quality  # noqa: E402

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def iso(hours_ago: float = 0, days_ago: float = 0) -> str:
    return (NOW - timedelta(hours=hours_ago, days=days_ago)).isoformat().replace("+00:00", "Z")


def story(sid, headline, first=iso(2), articles=None, count=None, image=None, pinned=False):
    arts = articles or [{"url": f"https://news.test/{sid}", "title": headline, "publishedAt": first, "isLead": True, "source": "News Test"}]
    return {"id": sid, "slug": f"s{sid}", "headline": headline, "firstPublishedAt": first, "updatedAt": first,
            "articleCount": count or len(arts), "articles": arts, "imageUrl": image, "pinned": pinned}


def test_old_news_catches_year_marker_and_old_articles():
    gpt2 = story(1, "Language Models are Unsupervised Multitask Learners (2019)")
    old = story(2, "A lab ships a model", articles=[{"url": "https://x.test/a", "title": "A lab ships a model", "publishedAt": iso(days_ago=40), "isLead": True}])
    url_dated = story(3, "Some analysis", articles=[{"url": "https://blog.test/2024/03/analysis", "title": "Some analysis", "publishedAt": None, "isLead": True}])
    fresh = story(4, "Fresh news today (2026)")
    followup = story(5, "Old launch gets new coverage", articles=[
        {"url": "https://x.test/b", "title": "t", "publishedAt": iso(days_ago=30), "isLead": False},
        {"url": "https://x.test/c", "title": "t", "publishedAt": iso(3), "isLead": True}])
    aged_out = story(6, "Paper (2019)", first=iso(days_ago=5))  # not shown as new any more
    flagged = {i["slug"]: i for i in quality.old_news([gpt2, old, url_dated, fresh, followup, aged_out], NOW)}
    assert set(flagged) == {"s1", "s2", "s3"}
    assert "(2019)" in flagged["s1"]["detail"] and flagged["s1"]["action"] == {"kind": "unpublish", "slug": "s1", "label": "Unpublish"}


def test_single_source_lead_and_audio_mismatch():
    lead = story(1, "Blog post about agents")
    other = story(2, "Big launch", articles=[{"url": "u1", "isLead": True}, {"url": "u2"}])
    by_id = {1: lead, 2: other}
    briefing = {"date": "2026-09-14", "storyIds": [1, 2]}
    assert quality.single_source_lead(briefing, by_id)["slug"] == "s1"
    assert quality.single_source_lead({"storyIds": [2, 1]}, by_id) is None
    assert quality.single_source_lead(briefing, {1: {**lead, "pinned": True}}) is None
    eps = [{"date": "2026-09-14", "stories": [{"slug": "s2", "headline": "Big launch"}]}]
    assert quality.audio_mismatch(eps, briefing, by_id)["slug"] == "s2"
    assert quality.audio_mismatch(eps, {"date": "2026-09-14", "storyIds": [2]}, by_id) is None
    assert quality.audio_mismatch([{**eps[0], "date": "2026-09-13"}], briefing, by_id) is None  # yesterday's audio: no claim


def test_over_merged_and_duplicates():
    big = story(1, "Mistral raises money", count=446)
    ok = story(2, "Normal story", count=12)
    assert [i["slug"] for i in quality.over_merged([ok, big])] == ["s1"]

    a = story(10, "OpenAI launches Agents API beta for long-running cloud agents", count=5)
    b = story(11, "OpenAI launches Agents API beta for long running cloud agents", count=2)
    c = story(12, "Nvidia launches new chips for cloud data centers", count=3)
    old = story(13, "OpenAI launches Agents API beta for long-running cloud agents", first=iso(days_ago=4))
    dups = quality.duplicates([a, b, c, old], NOW)
    assert [d["slug"] for d in dups] == ["s11"]  # the copy with fewer sources; the 4-day-old one is not live
    assert "5 sources" in dups[0]["detail"]
    assert quality.headline_similarity("Meta buys a robotics startup", "Apple sues a chip designer") < 0.5


def test_tracker_gaps():
    trackers = {"models": [{"name": "X-1", "lab": "unknown", "kind": "llm", "storySlug": "a"},
                           {"name": "Y-2", "lab": "Acme", "kind": "other", "storySlug": "b"},
                           {"name": "Z-3", "lab": "Acme", "kind": "llm", "storySlug": "c"}],
                "funding": [{"company": "Nscale", "round": "other", "storySlug": "d"}, {"company": "unknown", "round": "seed", "storySlug": "e"}]}
    gaps = quality.tracker_gaps(trackers)
    assert [g["headline"] for g in gaps] == ["X-1", "unknown"]  # "other" is a real type, not a gap
    assert gaps[1]["page"] == "/funding"


def test_network_checks_are_bounded_and_classified():
    import time

    page = [story(1, "One", image="https://img.test/1.jpg"), story(2, "Two", image="https://cryptobriefing.com/2.jpg"),
            story(3, "Three", image="https://img.test/slow.jpg")]
    page[0]["articles"][0]["url"] = "https://gone.test/article"
    codes = {"https://img.test/1.jpg": 200, "https://cryptobriefing.com/2.jpg": 403, "https://gone.test/article": 404}

    def fake(url):
        if "slow" in url:
            time.sleep(2)
        return codes.get(url, 200)

    t0 = time.time()
    statuses = quality.check_urls([s["imageUrl"] for s in page] + ["https://gone.test/article", "not-a-url"], fake, deadline=0.5)
    assert time.time() - t0 < 1.5 and "https://img.test/slow.jpg" not in statuses
    imgs = quality.broken_images(page, statuses)
    assert [i["slug"] for i in imgs] == ["s2"] and "403" in imgs[0]["detail"] and "cryptobriefing.com" in imgs[0]["detail"]
    links = quality.broken_links(page, statuses, [])
    assert [i["slug"] for i in links] == ["s1"] and links[0]["action"]["kind"] == "unpublish"


def test_cards_are_plain_and_skip_empty_flags():
    flags = {"oldNews": [{"slug": "s1", "headline": "GPT-2 (2019)", "detail": "d"}], "singleLead": None, "overMerged": [],
             "duplicates": [], "brokenImages": [], "brokenLinks": [], "trackerGaps": [], "audio": None}
    out = quality.cards(flags)
    assert [c["id"] for c in out] == ["quality:old_news"]
    assert out[0]["what"] == "An old story is on the site as new." and out[0]["why"] and out[0]["todo"]


def test_run_cards_translate_failures():
    from digest import admin

    def run(minutes_ago, crashed_step=None):
        start = NOW - timedelta(minutes=minutes_ago)
        steps = [{"step": "fetch", "startedAt": start, "stats": {}}, {"step": "enrich", "startedAt": start + timedelta(minutes=2), "stats": {}}]
        if crashed_step:
            steps.append({"step": crashed_step, "startedAt": start + timedelta(minutes=5), "stats": {"crashed": True}})
        return steps

    rows = run(600, "enrich") + run(60) + run(30) + run(0)
    cards = admin.run_cards(rows, NOW)
    assert len(cards) == 1 and cards[0]["level"] == "info"
    assert "{at}" in cards[0]["what"] and "writing summaries" in cards[0]["what"] and "no action needed" in cards[0]["todo"].lower()

    rows = run(90) + run(60, "export") + run(30, "export") + run(0, "export")
    cards = admin.run_cards(rows, NOW)
    assert cards[0]["level"] == "critical" and "last 3 runs" in cards[0]["what"]
    assert cards[0]["action"]["kind"] == "link"
    assert admin.run_cards(run(30) + run(0), NOW) == []


def test_search_cards():
    from digest import admin

    gsc = {"property": "https://digestai.news/", "sitemaps": [
        {"path": "/sitemap-index.xml", "submitted": 285, "indexed": 0, "errors": "0"},
        {"path": "/news-sitemap.xml.", "submitted": 0, "indexed": 0, "errors": "1"}],
        "inspections": [{"page": "/", "state": "Submitted and indexed"}, {"page": "/today", "state": "Crawled - currently not indexed"},
                        {"page": "/models", "state": "URL is unknown to Google"}]}
    steps = [{"step": "gsc", "startedAt": NOW - timedelta(hours=30), "stats": {"configured": True, "clicks": 0}},
             {"step": "gsc", "startedAt": NOW - timedelta(hours=1), "stats": {"configured": True, "error": "auth"}}]
    cards = {c["id"]: c for c in admin.search_cards(gsc, steps, NOW)}
    card = cards["search:indexed"]
    assert "1 of 3" in card["what"] and card["level"] == "info"  # the home page is indexed
    assert [i["headline"] for i in card["items"]][:2] == ["/today", "/models"]
    assert "search-console/inspect" in card["items"][1]["action"]["url"] and "%2Fmodels" in card["items"][1]["action"]["url"]
    assert any(i["headline"] == "/news-sitemap.xml." for i in card["items"])
    assert "search:stale" in cards and "{at}" in cards["search:stale"]["what"]
    fresh = [{"step": "gsc", "startedAt": NOW - timedelta(hours=1), "stats": {"configured": True, "clicks": 3}}]
    fine = {"sitemaps": [{"path": "/s.xml", "submitted": 500, "indexed": 0, "errors": "0"}],
            "inspections": [{"page": "/", "state": "Submitted and indexed"}]}
    assert admin.search_cards(fine, fresh, NOW) == []  # the sitemap's 0 indexed is ignored: Google no longer fills it in
    home = {"inspections": [{"page": "/", "state": "URL is unknown to Google"}]}
    assert admin.search_cards(home, fresh, NOW)[0]["level"] == "warning"


def test_summary_cards_only_when_readers_wait():
    from digest import admin

    def enrich(minutes_ago, enriched=0, budget=None, crashed=False):
        stats = {"crashed": True} if crashed else {"enriched": enriched, "rejected": 0, "budget": budget if budget is not None else {"gemini": 0, "groq": 20}}
        return {"step": "enrich", "startedAt": NOW - timedelta(minutes=minutes_ago), "stats": stats}

    old = NOW - timedelta(hours=3)
    # Gemini is out for the day but Groq keeps writing: normal, no card.
    assert admin.summary_cards([enrich(60, 8), enrich(30, 6), enrich(0, 7)], {"gemini"}, 12, old, NOW) == []
    # Nothing waiting, or only articles that just arrived: no card, even after quiet runs.
    quiet = [enrich(60), enrich(30), enrich(0)]
    assert admin.summary_cards(quiet, set(), 0, None, NOW) == []
    assert admin.summary_cards(quiet, set(), 5, NOW - timedelta(minutes=20), NOW) == []
    # Two quiet runs with a model still available: not yet.
    assert admin.summary_cards([enrich(60, 4), enrich(30), enrich(0)], set(), 5, old, NOW) == []
    # Three runs in a row wrote nothing while articles waited.
    cards = admin.summary_cards(quiet, set(), 5, old, NOW)
    assert [c["id"] for c in cards] == ["summaries:stopped"] and cards[0]["level"] == "warning"
    assert "last 3 runs" in cards[0]["what"] and "5 articles" in cards[0]["what"] and cards[0]["action"]["kind"] == "link"
    # Every model is out of its allowance: one card at once, calmer late in the day.
    out = [enrich(30, 5), enrich(0, 0, {"gemini": 0, "groq": 0})]
    cards = admin.summary_cards(out, {"gemini"}, 9, old, NOW)
    assert [c["id"] for c in cards] == ["summaries:allowance"] and cards[0]["level"] == "warning" and "12 hours" in cards[0]["why"]
    groq_ran_out = [enrich(0, 0, {"gemini": 0, "groq": 20})]
    assert admin.summary_cards(groq_ran_out, {"gemini", "groq"}, 9, old, NOW)[0]["id"] == "summaries:allowance"
    late = NOW.replace(hour=22)
    assert admin.summary_cards([dict(r, startedAt=late) for r in out], {"gemini"}, 9, old, late)[0]["level"] == "info"
    # A local model with a share left keeps writing: not all out. A crashed step is left to the run cards.
    assert admin.summary_cards([enrich(0, 0, {"gemini": 0, "groq": 0, "ollama": 30})], set(), 9, old, NOW) == []
    assert admin.summary_cards([enrich(0, crashed=True)], set(), 9, old, NOW) == []


def test_site_search_cards():
    from digest import admin

    summary = {"missing": [{"query": "robot dogs", "searches": 4, "visitors": 3, "results": 0},
                           {"query": "typo", "searches": 2, "visitors": 1, "results": 0}]}
    cards = admin.site_search_cards(summary)
    assert len(cards) == 1 and cards[0]["id"] == "searches:missing" and cards[0]["level"] == "info"
    assert '"robot dogs"' in cards[0]["what"] and [i["headline"] for i in cards[0]["items"]] == ["robot dogs"]
    assert admin.site_search_cards({"missing": summary["missing"][1:]}) == [] and admin.site_search_cards(None) == []


def test_ranking_card_needs_enough_impressions():
    from digest import admin

    def days(before, now, imp=10):
        return {"perDay": [{"day": f"2026-09-{i + 1:02d}", "impressions": imp, "clicks": 0, "position": before if i < 7 else now} for i in range(14)]}

    up = admin.ranking_cards(days(30.0, 12.0))
    assert len(up) == 1 and up[0]["level"] == "info" and "higher" in up[0]["what"] and "page 2" in up[0]["why"]
    assert "lower" in admin.ranking_cards(days(8.0, 20.0))[0]["what"]
    assert admin.ranking_cards(days(30.0, 12.0, imp=5)) == []  # 35 impressions a week: too few to judge
    assert admin.ranking_cards(days(30.0, 28.0)) == []  # a small move
    # A day without impressions has no position and does not count.
    gap = days(30.0, 12.0)
    gap["perDay"][10].update(impressions=0, position=None)
    assert "12.0" in admin.ranking_cards(gap)[0]["what"]
    assert admin.ranking_cards({"perDay": gap["perDay"][:10]}) == []


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
