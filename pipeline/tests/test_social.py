"""Bluesky posts (social.py): the text, the card and when a story goes out. Offline, no database and
nothing is posted: python tests/test_social.py"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import images, social  # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
EVENING = NOW.replace(hour=18)
LONG = ("Anthropic released Claude Sonnet 5 on Tuesday, a model it says matches its largest system on coding "
        "tests at a fifth of the price, and put it in the free tier of its chat app the same day.")


def story(sid: int = 1, **over) -> dict:
    s = {"id": sid, "slug": f"story-{sid}", "headline": "Anthropic ships Sonnet 5 at a fifth of the price",
         "summaryMd": f"{LONG} Developers can use it through the API from today.\n\nA second paragraph follows.",
         "keyPoints": ["The model costs $3 per million input tokens.", "It scored 71% on the coding test."],
         "whyItMatters": "Small teams can run coding agents for less than a cheap subscription.",
         "articleCount": 4, "importance": 7, "score": 0.5, "imageUrl": "https://example.com/a.jpg",
         "firstPublishedAt": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")}
    s.update(over)
    return s


def test_lede_reaches_the_length_that_performs_in_whole_sentences():
    text = social._lede(story(), social.TEXT_LIMIT)
    assert text.startswith(LONG)
    assert social.TARGET_CHARS <= len(text) <= social.TEXT_LIMIT, len(text)
    assert not text.endswith("…") and text[-1] in ".?!"


def test_lede_falls_back_to_the_headline_and_builds_from_key_points():
    s = story(summaryMd="Short opener. Then more.")
    text = social._lede(s, social.TEXT_LIMIT)
    assert text.startswith("Anthropic ships Sonnet 5 at a fifth of the price. The model costs $3"), text
    assert "71%" in text


def test_lede_never_speaks_as_the_source_writer():
    s = story(summaryMd=f"The author argues that {LONG}", keyPoints=["I think this changes everything for us.", "It scored 71% on the test."])
    text = social._lede(s, social.TEXT_LIMIT)
    assert "author" not in text and "I think" not in text and "71%" in text


def test_lede_leaves_out_a_key_point_that_repeats_the_opening():
    repeat = "Anthropic released Claude Sonnet 5 on Tuesday at a fifth of the price of its largest system."
    text = social._lede(story(keyPoints=[repeat, "It scored 71% on the coding test."]), social.TEXT_LIMIT)
    assert repeat not in text and "71%" in text


def test_lede_leaves_out_what_does_not_fit_instead_of_cutting_it():
    text = social._lede(story(keyPoints=["x " * 200 + "end.", "It scored 71% on the coding test."]), social.TEXT_LIMIT)
    assert "x x" not in text and "…" not in text


def test_a_short_limit_still_gives_a_whole_post():
    text = social._lede(story(summaryMd="Short."), 120)
    assert len(text) <= 120 and not text.endswith(",")


def test_story_post_for_a_work_item_names_the_reader():
    post = social._story_post(story(workCard={"tool": "Canva"}))
    assert post["text"].startswith("For marketers: ") and len(post["text"]) <= social.TEXT_LIMIT
    assert "utm_source=bluesky" in post["link"] and post["kind"] == "story"


def test_card_says_something_new_and_names_the_sources():
    post = social._story_post(story())
    assert post["description"].startswith("Small teams can run coding agents")
    assert post["description"].endswith("4 sources on one page.") and len(post["description"]) <= 280
    single = social._story_post(story(articleCount=1))
    assert "sources on one page" not in single["description"]


def test_briefing_post_only_promises_a_listen_that_exists():
    stories = {i: story(i) for i in (1, 2, 3, 4)}
    briefing = {"date": "2026-10-30", "storyIds": [1, 2, 3, 4], "stats": {"minutes": 6}}
    silent = social._briefing_post(briefing, stories, "Fri 30 Oct")
    assert "listen" not in silent["text"] and "Plus 3 more stories" in silent["text"]
    spoken = social._briefing_post(briefing, stories, "Fri 30 Oct", 5)
    assert "a 5-minute listen" in spoken["text"] and len(spoken["text"]) <= social.TEXT_LIMIT
    assert social._briefing_post({"date": "2026-10-30", "storyIds": [1, 2]}, stories, "x") is None


def test_briefing_post_uses_the_days_card_when_it_exists():
    stories = {i: story(i) for i in (1, 2, 3)}
    briefing = {"date": "2026-10-30", "storyIds": [1, 2, 3]}
    with tempfile.TemporaryDirectory() as tmp:
        out, images.OUT = images.OUT, Path(tmp)
        try:
            plain = social._briefing_post(briefing, stories, "x")
            assert plain["photo"] == "https://example.com/a.jpg" and plain["image"].name == "og-default.png"
            images.today_path("2026-10-30").write_bytes(b"png")
            carded = social._briefing_post(briefing, stories, "x")
            assert carded["photo"] is None and carded["image"].name == "today-2026-10-30.png"
        finally:
            images.OUT = out


def test_ordinary_stories_wait_for_the_evening():
    assert social._next_story([story()], set(), NOW, None, 0) is None
    picked = social._next_story([story()], set(), EVENING, None, 0)
    assert picked and picked["slug"] == "story-1"


def test_the_evening_can_still_post_the_mornings_news():
    morning = story(firstPublishedAt=(EVENING - timedelta(hours=10)).isoformat().replace("+00:00", "Z"))
    assert social._next_story([morning], set(), EVENING, None, 0)
    old = story(firstPublishedAt=(EVENING - timedelta(hours=13)).isoformat().replace("+00:00", "Z"))
    assert social._next_story([old], set(), EVENING, None, 0) is None


def test_a_very_important_story_goes_at_once():
    big = story(importance=8)
    assert social._next_story([big], set(), NOW, None, 0)
    # Even right after another post, and in the recap's run.
    assert social._next_story([big], set(), NOW, NOW - timedelta(minutes=5), 0, briefing_due=True)


def test_evening_posts_stay_apart_and_give_way_to_the_recap():
    assert social._next_story([story()], set(), EVENING, EVENING - timedelta(minutes=20), 0) is None
    assert social._next_story([story()], set(), EVENING, EVENING - timedelta(minutes=40), 0)
    assert social._next_story([story()], set(), EVENING, None, 0, briefing_due=True) is None


def test_the_bar_and_the_daily_cap_still_hold():
    assert social._next_story([story(articleCount=1, importance=7)], set(), EVENING, None, 0) is None
    assert social._next_story([story(importance=5)], set(), EVENING, None, 0) is None
    assert social._next_story([story()], {("story", "story-1")}, EVENING, None, 0) is None
    assert social._next_story([story()], set(), EVENING, None, social.config.SOCIAL_MAX_PER_DAY) is None


def test_best_story_goes_first():
    picked = social._next_story([story(1, score=0.2), story(2, score=0.9)], set(), EVENING, None, 0)
    assert picked["id"] == 2


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
