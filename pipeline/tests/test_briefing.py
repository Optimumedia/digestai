"""The briefing's order (export.build_briefing), the community-traction term in the story score
(rank.traction) and the cluster ceiling (cluster.pick_story, cluster.split_by_centroid).
Offline, no database: python tests/test_briefing.py"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from digest import cluster, export, morning, primary, rank, share  # noqa: E402

NOW = datetime(2026, 9, 21, 5, 30, tzinfo=timezone.utc)


def iso(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def art(domain, source_type="press", url=None, key=None, content_type="news"):
    return {"domain": domain, "sourceType": source_type, "url": url or f"https://{domain}/a", "sourceKey": key,
            "contentType": content_type, "publishedAt": iso(2)}


def story(i, score, arts, category="models", pinned=False, hours=2):
    return {"id": i, "score": score, "pinned": pinned, "category": category, "firstPublishedAt": iso(hours),
            "hasPrimary": any(a["sourceType"] == "primary" for a in arts), "articles": arts,
            "articleCount": len(arts), "summaryMd": "w " * 40}


def paper(i, score, key="arxiv-cs-cl"):
    return story(i, score, [art("arxiv.org", "primary", "https://arxiv.org/abs/2609.01234", key, "research")], "research")


def one(i, score, category="models"):
    return story(i, score, [art(f"outlet{i}.com")], category)


# ---------------------------------------------------------------------------- what is primary

def test_only_a_company_announcement_confirms_a_story_on_its_own():
    lab = art("openai.com", "primary", "https://openai.com/index/sponsored-agents")
    assert primary.announcement(lab) and export.confirmed(story(1, 0.4, [lab]))
    for a in (art("arxiv.org", "primary", "https://arxiv.org/abs/2609.1", "arxiv-cs-cl", "research"),
              art("nature.com", "primary", "https://www.nature.com/articles/s1", "nature-ml"),
              art("news.mit.edu", "primary", "https://news.mit.edu/2026/x", "mit-news-ai"),
              art("github.com", "primary", "https://github.com/acme/model"),
              art("huggingface.co", "primary", "https://huggingface.co/acme/model-7b"),
              art("whitehouse.gov", "primary", "https://www.whitehouse.gov/wp-content/uploads/plan.pdf"),
              art("openai.com", "primary", "https://openai.com/index/paper", content_type="research")):
        s = story(2, 0.4, [a])
        assert s["hasPrimary"] and not primary.announcement(a) and not export.confirmed(s), a
        assert not morning.confirmed(s) and not share.confirmed(s), a
    assert primary.announcement(art("huggingface.co", "primary", "https://huggingface.co/blog/smol"))
    # The export's flag is used when present; two publishers confirm as before.
    assert export.confirmed({"articles": [art("a.com")], "hasAnnouncement": True})
    assert export.confirmed(story(3, 0.4, [art("reuters.com"), art("theverge.com")]))


def test_a_paper_never_leads_the_briefing():
    # 21 Sep: DischargeBench (arXiv cs.CL, one source) led because arXiv counted as the primary source.
    stories = [one(1, 0.461), paper(2, 0.407), one(3, 0.40), one(4, 0.39), one(5, 0.38)]
    stories += [one(i, 0.3 - i * 0.001) for i in range(10, 25)]
    top = export.build_briefing(stories, NOW)["storyIds"]
    # The paper confirms nothing, so the score order stands; second on score, it starts at third.
    assert top == [1, 3, 2, 4, 5], top


def test_a_research_story_starts_at_third_place_unless_pinned():
    confirmed_paper = story(2, 0.9, [art("arxiv.org", "primary", "https://arxiv.org/abs/1", "arxiv-cs-ai", "research"),
                                     art("theverge.com"), art("wired.com")], "research")
    stories = [confirmed_paper, one(1, 0.8), one(3, 0.7), one(4, 0.6), one(5, 0.5), one(6, 0.4)]
    stories += [one(i, 0.3 - i * 0.001) for i in range(10, 25)]
    top = export.build_briefing(stories, NOW)["storyIds"]
    assert top.index(2) == 2, top  # third, above the single-outlet stories it outranks
    # Two research stories at the top: both go to third place and below, in score order.
    two = [confirmed_paper, story(7, 0.85, [art("nature.com", "primary", key="nature-ml"), art("ft.com")], "research")] + stories[1:]
    top = export.build_briefing(two, NOW)["storyIds"]
    assert top[:2] == [1, 3] and top[2:4] == [2, 7], top
    # A pin is the owner's call.
    pinned = [dict(confirmed_paper, pinned=True)] + stories[1:]
    assert export.build_briefing(pinned, NOW)["storyIds"][0] == 2
    # A research story at third place or below is not moved.
    low = [one(1, 0.9), one(3, 0.8), confirmed_paper | {"score": 0.7}, one(4, 0.6), one(5, 0.5)] + stories[6:]
    assert export.build_briefing(low, NOW)["storyIds"][:3] == [1, 3, 2]


def test_papers_only_briefing_still_puts_other_news_first_and_keeps_the_focus_place():
    papers = [paper(i, 0.9 - i * 0.01) for i in range(1, 6)]
    rest = [one(20, 0.2), one(21, 0.19), one(22, 0.18, "marketing")]
    top = export.build_briefing(papers + rest, NOW)["storyIds"]
    # The marketing story holds the focus place in the five, so it and the best other story lead;
    # the papers follow in score order and the lowest of them gives way.
    assert top == [22, 20, 1, 2, 3], top


def test_the_morning_note_names_the_lab_not_the_paper():
    s = story(1, 0.5, [art("arxiv.org", "primary", key="arxiv-cs-cl") | {"source": "arXiv cs.CL"},
                       art("openai.com", "primary", "https://openai.com/index/x") | {"source": "OpenAI"}])
    assert morning.primary_name(s) == "OpenAI"


# ---------------------------------------------------------------------------- community traction

def test_traction_is_bounded_log_scaled_and_ignores_small_threads():
    assert rank.traction(None) == rank.traction(0) == rank.traction(20) == rank.traction(rank.TRACTION_FLOOR) == 0.0
    assert rank.traction(5000) == rank.traction(rank.TRACTION_FULL) == 1.0
    values = [rank.traction(p) for p in (31, 50, 114, 250, 500, 676, 999)]
    assert values == sorted(values) and all(0 < v < 1 for v in values)
    # Log scale: each tenfold rise in points adds the same amount.
    step = lambda a, b: rank.traction(b) - rank.traction(a)  # noqa: E731
    assert math.isclose(step(60, 600), step(90, 900), rel_tol=0.02)
    assert rank.TRACTION_WEIGHT * rank.traction(20) == 0.0  # a 20-point thread does not move a story
    assert rank.TRACTION_WEIGHT * rank.traction(50) < 0.03


def test_a_big_hacker_news_thread_reaches_the_top_five_of_a_normal_day():
    # Stored scores of 21 Sep (admin story table): the 676-point story scored 0.38 and sat 31st.
    day = [0.49, 0.49, 0.49, 0.47, 0.47, 0.47, 0.46, 0.46, 0.46, 0.46, 0.45, 0.45, 0.45, 0.44, 0.44, 0.43, 0.43, 0.43, 0.42, 0.40]
    for points in (500, 676):
        boosted = 0.38 + rank.TRACTION_WEIGHT * rank.traction(points)
        assert sum(1 for s in day if s > boosted) < 5, (points, boosted)
    # A 20-point thread leaves the same story where it was.
    assert 0.38 + rank.TRACTION_WEIGHT * rank.traction(20) == 0.38


# ---------------------------------------------------------------------------- the cluster ceiling

def _unit(rng, base=None, noise=0.3, dim=64):
    v = rng.standard_normal(dim)
    if base is not None:
        v = base + noise * v / np.sqrt(dim)
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_a_story_at_the_ceiling_takes_no_more_articles():
    rng = np.random.default_rng(2)
    base = _unit(rng)
    v = _unit(rng, base=base, noise=0.1)
    stories = {1: {"vec": base, "lead_vec": base, "count": 80, "shown": 40}}
    assert cluster.pick_story(v, stories, 0.82, 0.79, max_total=80) == (None, -1.0)  # starts its own story
    stories[1]["count"] = 79
    assert cluster.pick_story(v, stories, 0.82, 0.79, max_total=80)[0] == 1  # overflow, below the ceiling
    # A second story on the event, below the ceiling, takes it instead.
    stories = {1: {"vec": base, "lead_vec": base, "count": 90}, 2: {"vec": base, "lead_vec": base, "count": 3}}
    assert cluster.pick_story(v, stories, 0.82, 0.79, max_total=80)[0] == 2


def test_split_by_centroid_keeps_the_lead_and_the_closest_members():
    rng = np.random.default_rng(4)
    base, other = _unit(rng), _unit(rng)
    members = [(1, _unit(rng, base=other, noise=0.2), True)]  # the lead, even far from the rest
    members += [(i, _unit(rng, base=base), i <= 20) for i in range(2, 50)]
    members += [(i, _unit(rng, base=other), False) for i in range(50, 60)]
    members += [(60, None, False)]
    keep, detach = cluster.split_by_centroid(1, members, 45)
    assert keep[0] == 1 and len(keep) == 45 and len(detach) == 15
    assert set(range(50, 61)) <= set(detach)  # the unrelated ones and the one without an embedding
    assert detach[0] == 60 and all(i < 50 for i in detach[11:])  # least similar first


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
