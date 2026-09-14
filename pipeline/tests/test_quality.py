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
                "funding": [{"company": "Nscale", "round": "other", "storySlug": "d"}, {"company": "Cognition", "round": "series_d_plus"}]}
    gaps = quality.tracker_gaps(trackers)
    assert [g["headline"] for g in gaps] == ["X-1", "Y-2", "Nscale"]
    assert gaps[2]["page"] == "/funding"


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

    gsc = {"property": "sc-domain:digestai.news", "sitemaps": [
        {"path": "/sitemap-index.xml", "submitted": 285, "indexed": 0, "errors": "0"},
        {"path": "/news-sitemap.xml.", "submitted": 0, "indexed": 0, "errors": "1"}]}
    steps = [{"step": "gsc", "startedAt": NOW - timedelta(hours=30), "stats": {"configured": True, "clicks": 0}},
             {"step": "gsc", "startedAt": NOW - timedelta(hours=1), "stats": {"configured": True, "error": "auth"}}]
    cards = {c["id"]: c for c in admin.search_cards(gsc, steps, NOW)}
    assert "0 of 285" in cards["search:indexed"]["what"]
    assert "search-console/inspect" in cards["search:indexed"]["action"]["url"]
    assert "search:stale" in cards and "{at}" in cards["search:stale"]["what"]
    fresh = [{"step": "gsc", "startedAt": NOW - timedelta(hours=1), "stats": {"configured": True, "clicks": 3}}]
    few = {"sitemaps": [{"path": "/s.xml", "submitted": 5, "indexed": 0, "errors": "0"}]}
    assert admin.search_cards(few, fresh, NOW) == []  # below the minimum volume, and fresh


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
