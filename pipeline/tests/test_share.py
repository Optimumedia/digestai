"""Share today (share.py): the daily shortlist for LinkedIn and X, the ready-made posts and the
done-state file. Offline, no database: python tests/test_share.py"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import share  # noqa: E402

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
HYPE = re.compile(r"revolution|game[- ]?chang|groundbreaking|ground-breaking|unleash|mind-blowing|jaw-dropping|earth-shattering|!", re.I)


def story(sid: int, category: str = "models", domains=("a.com", "b.com"), **over) -> dict:
    s = {
        "id": sid, "slug": f"story-{sid}", "headline": f"Lab ships model number {sid} for coding",
        "summaryMd": "A lab shipped a model. It writes code.", "category": category, "categoryName": category.title(),
        "keyPoints": ["The model scores 71% on the coding test.", "It is free for researchers."],
        "whyItMatters": "Coding tools get cheaper for small teams. More detail follows.",
        "entities": {"companies": ["Acme"], "models": [], "people": []},
        "importance": 7, "score": 0.5, "pinned": False, "hasPrimary": False,
        "firstPublishedAt": "2026-09-21T08:00:00Z",
        "articles": [{"domain": d, "title": f"Lab ships model {sid}", "isLead": i == 0} for i, d in enumerate(domains)],
    }
    s.update(over)
    return s


def work_story(sid: int) -> dict:
    return story(sid, "marketing", domains=("c.com",), headline="Canva adds a brief-to-post writer",
                 workCard={"tool": "Canva", "whatItDoes": "Writes social posts from a short brief.", "cost": "Free tier",
                           "costKind": "free tier", "watchOut": "The free tier watermarks video exports."})


def day_of_stories():
    stories = [story(1, domains=("a.com",)), story(2), story(3, hasPrimary=True, domains=("openai.com",)),
               story(4), story(5), story(6, "policy", importance=8), story(7, "research", importance=5), work_story(8),
               story(9, "hardware", firstPublishedAt="2026-09-01T00:00:00Z", importance=9)]
    briefing = {"date": "2026-09-21", "storyIds": [1, 2, 3, 4, 5], "alsoIds": [6, 7]}
    work_briefing = {"storyIds": [8], "featuredId": 8}
    return stories, briefing, work_briefing


# ---------------------------------------------------------------------------- the shortlist

def test_shortlist_leads_with_confirmed_briefing_stories_then_the_work_pick_and_variety():
    stories, briefing, work_briefing = day_of_stories()
    items = share.shortlist(stories, briefing, work_briefing, {"stories": {}}, NOW)
    ids = [p["story"]["id"] for p in items]
    assert share.MIN_ITEMS <= len(items) <= share.MAX_ITEMS, ids
    # Story 1 led the briefing from one outlet: the confirmed ones (2, 3, 4) go first, four at most.
    assert ids[:4] == [2, 3, 4, 5], ids
    assert items[0]["reason"] == "No. 2 in the briefing · 2 publishers", items[0]["reason"]
    assert items[1]["reason"].endswith("the lab's own post")
    kinds = {p["kind"]: p for p in items}
    assert kinds["work"]["story"]["id"] == 8 and kinds["work"]["reason"].startswith("AI at Work pick")
    variety = kinds["variety"]["story"]
    assert variety["id"] == 6 and variety["category"] not in {"models", "marketing"}, "a category not on the list yet"
    assert 9 not in ids, "an old story outside the briefing is never offered"
    assert all(p["reason"] for p in items)


def test_shortlist_skips_what_was_shared_on_an_earlier_day_and_keeps_todays():
    stories, briefing, work_briefing = day_of_stories()
    shared = {"stories": {
        "2": {"linkedin": {"at": "2026-09-20T18:00:00Z", "url": ""}},  # yesterday: drops off
        "8": {"x": {"at": "2026-09-19T09:00:00Z"}},                    # the work pick, two days ago
        "3": {"x": {"at": "2026-09-21T09:30:00Z", "url": "https://x.com/DigestAINews/status/1"}},  # today: stays, done
    }}
    items = share.shortlist(stories, briefing, work_briefing, shared, NOW)
    ids = [p["story"]["id"] for p in items]
    assert 2 not in ids and 8 not in ids, ids
    assert 3 in ids, "shared today stays on today's list, marked done"
    assert not any(p["kind"] == "work" for p in items), "no other work story to offer"
    out = share.build(stories, briefing, work_briefing, shared, NOW)
    done = next(i for i in out["items"] if i["id"] == 3)["done"]
    assert done == {"x": {"at": "2026-09-21T09:30:00Z", "url": "https://x.com/DigestAINews/status/1"}}, done


def test_empty_briefing_falls_back_to_the_days_best_by_score():
    stories = [story(i, score=i / 10) for i in range(1, 8)]
    items = share.shortlist(stories, {"storyIds": [], "alsoIds": []}, {}, {"stories": {}}, NOW)
    assert [p["story"]["id"] for p in items] == [7, 6, 5, 4, 3], "one category, so no variety pick: best score first"
    assert items[0]["reason"] == "Top by score today · 2 publishers"


# ---------------------------------------------------------------------------- the posts

def test_x_length_counts_links_as_23_and_wide_characters_twice():
    assert share.x_length("hello https://digestai.news/story/" + "a" * 200) == 6 + 23
    assert share.x_length("AI 模型") == 3 + 4
    long = story(1, headline="A very long headline " * 20, whyItMatters="Why " * 100, keyPoints=["Point " * 80])
    for s in (long, story(2), work_story(3), story(4, headline="日本の研究所が新しいモデルを公開 " * 8)):
        text = share.x_post(s)
        assert share.x_length(text) <= share.X_LIMIT, (share.x_length(text), text)
        assert text.rstrip().endswith(share.story_url(s["slug"], "x")), "the link goes last"
    assert len(share.x_post(long)) > share.X_LIMIT, "the raw length may pass 280: the t.co rule is what counts"


def test_links_carry_the_utm_tags_for_each_network():
    s = story(5)
    for net in ("linkedin", "x"):
        url = share.story_url(s["slug"], net)
        q = parse_qs(urlparse(url).query)
        assert urlparse(url).path == "/story/story-5"
        assert q == {"utm_source": [net], "utm_medium": ["social"], "utm_campaign": ["share-today"]}, q
    assert share.story_url(s["slug"], "linkedin") in share.linkedin_post(s)
    assert share.story_url(s["slug"], "x") in share.x_post(s)
    assert "utm_" not in share.story_url(s["slug"])


def test_posts_carry_no_hype_and_at_most_three_hashtags():
    s = story(6, headline="Acme unleashes revolutionary game-changer model",
              keyPoints=["A groundbreaking model that revolutionizes coding!", "It is a mind-blowing release."],
              whyItMatters="This game-changing launch unleashes cheaper tools.")
    li, x = share.linkedin_post(s), share.x_post(s)
    for text in (li, x):
        assert not HYPE.search(text), text
    assert "releases" in li, "swapped for the plain word, not just deleted"
    assert len(re.findall(r"#\w+", li)) <= 3 and len(re.findall(r"#\w+", x)) <= 2
    assert re.findall(r"#\w+", share.linkedin_post(work_story(7))) == ["#Marketing", "#SmallBusiness", "#Acme"]


def test_linkedin_post_has_a_hook_substance_and_a_call_to_read():
    li = share.linkedin_post(story(8, domains=("a.com", "b.co.uk", "news.a.com")))
    lines = li.split("\n")
    assert lines[0] == "Lab ships model number 8 for coding.", lines[0]
    assert "• The model scores 71% on the coding test." in lines
    assert "Why it matters: Coding tools get cheaper for small teams." in lines, "first sentence only"
    assert "The full story, with all 2 sources linked:" in li, "a.com and news.a.com are one publisher"
    w = share.linkedin_post(work_story(9))
    assert w.startswith("For marketers and small teams: Canva adds a brief-to-post writer.")
    assert "Cost: Free tier." in w and "Worth knowing: The free tier watermarks video exports." in w


# ---------------------------------------------------------------------------- the done-state file

def test_the_done_state_file_is_read_and_counted():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        path = d / "shared.json"
        path.write_text(json.dumps({"stories": {
            "2": {"slug": "story-2", "linkedin": {"at": "2026-09-20T18:00:00Z", "url": "https://www.linkedin.com/feed/update/1"},
                  "x": {"at": "2026-09-21T08:00:00Z"}},
            "4": {"x": {"at": "2026-09-01T08:00:00Z"}},       # older than a week
            "bad": {"x": {"at": "2026-09-21T08:00:00Z"}},     # not a story id: ignored
            "5": "nonsense",
        }}), encoding="utf-8")
        shared = share.load_shared(path)
        assert share.shared_before(shared, "2026-09-21") == {2, 4}
        assert share.week_counts(shared, NOW) == {"linkedin": 1, "x": 1}
        stories, briefing, work_briefing = day_of_stories()
        for name, data in (("stories.json", stories), ("briefing.json", briefing), ("work-briefing.json", work_briefing)):
            (d / name).write_text(json.dumps(data), encoding="utf-8")
        out = share.run_from_files(NOW, data_dir=d, shared_path=path)
        assert 2 not in [i["id"] for i in out["items"]] and out["week"] == {"linkedin": 1, "x": 1}
        assert out["file"] == "pipeline/digest/shared.json"
        path.write_text("{ broken", encoding="utf-8")
        assert share.load_shared(path) == {"stories": {}}
        assert share.load_shared(d / "missing.json") == {"stories": {}}
    assert isinstance(share.load_shared()["stories"], dict), "the committed file parses"


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
