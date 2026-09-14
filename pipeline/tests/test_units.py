"""Offline unit tests: python -m pytest tests/ or python tests/test_units.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest.extract import scrub, validate  # noqa: E402
from digest.textutil import clean_title, hamming, keywords, normalize_url, simhash, slugify  # noqa: E402


def test_clean_title_strips_source_suffix_and_ellipsis():
    assert clean_title("Nvidia Introduces First PCs Designed for AI Agents - WSJ") == "Nvidia Introduces First PCs Designed for AI Agents"
    assert clean_title("IREN's partnership with NVIDIA expands with $3.4 billion AI cloud ...") == "IREN's partnership with NVIDIA expands with $3.4 billion AI cloud"
    # Short remainders keep their suffix: "AI - Wikipedia" is not a source suffix worth cutting.
    assert clean_title("Why AI matters - Wikipedia") == "Why AI matters - Wikipedia"


def test_normalize_url_drops_tracking_and_www():
    assert normalize_url("https://www.example.com/a/b/?utm_source=x&id=3#frag") == "https://example.com/a/b?id=3"


BING_LINK = ("http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=6aa82acf439047288f023edceb8b9cef"
             "&url=https%3a%2f%2fwww.theverge.com%2fai%2f123%2fopenai-model%3futm_source%3dbing&c=18124483902249883319&mkt=en-ww")


def test_normalize_url_unwraps_bing_click_links():
    from digest.textutil import is_skipped_domain, unwrap_redirect

    assert normalize_url(BING_LINK) == "https://theverge.com/ai/123/openai-model"
    # tid and c change on every fetch; the same article must normalise to the same URL.
    again = BING_LINK.replace("6aa82acf439047288f023edceb8b9cef", "ffff").replace("18124483902249883319", "42")
    assert normalize_url(again) == normalize_url(BING_LINK)
    assert unwrap_redirect("https://bing.com/news/search?q=x") == "https://bing.com/news/search?q=x"
    assert unwrap_redirect("http://bing.com/news/apiclick.aspx?url=javascript%3aalert(1)").startswith("http://bing.com")
    assert is_skipped_domain("msn.com") and is_skipped_domain("en.msn.com") and is_skipped_domain("bing.com")
    assert not is_skipped_domain("research.google.com") and not is_skipped_domain("notmsn.com")


def test_fetch_near_duplicate_merges_discussion():
    from digest.fetch import near_duplicate, should_merge_discussion

    title = simhash("OpenAI restricts GPT-2 release over malicious use concerns")
    recent = [{"id": 7, "hash": simhash("Humanoid robots enter the warehouse"), "domain": "a.com", "discussion_url": None, "points": None},
              {"id": 9, "hash": title, "domain": "openai.com", "discussion_url": None, "points": None}]
    dup = near_duplicate(simhash("OpenAI restricts GPT-2 release over malicious use concerns, report says"), recent)
    assert dup is not None and dup["id"] == 9
    assert near_duplicate(simhash("Nvidia ships a new data center GPU"), recent) is None
    hn = ("hn", "https://news.ycombinator.com/item?id=1", 120)
    assert should_merge_discussion(hn, dup)  # no thread yet
    assert not should_merge_discussion(hn, {**dup, "discussion_url": "x", "points": 300})
    assert should_merge_discussion(hn, {**dup, "discussion_url": "x", "points": 50})
    assert not should_merge_discussion(None, dup)


def test_discovery_terms_only_entities_and_not_generic():
    from types import SimpleNamespace

    from digest.rank import discovery_key, discovery_terms, is_discovery_term

    def art(companies, models=(), points=100):
        return SimpleNamespace(entities={"companies": list(companies), "models": list(models)}, engagement=0.0,
                               discussion_points=points, trend_score=0, headline="Some headline about agents")
    top = [art(["Mistral AI", "Scaleup Europe Fund"]), art(["Mistral AI", "The Information"]),
           art(["Samsung Electronics"], ["GPT‑6 Astra"]), art(["Anthropic"], ["GPT‑6 Astra"]), art(["Anthropic"])]
    terms = discovery_terms(top, min_articles=2, limit=8)
    assert "Mistral AI" in terms and "Anthropic" in terms and "GPT-6 Astra" in terms
    assert "Scaleup Europe Fund" not in terms and "The Information" not in terms
    assert "Samsung Electronics" not in terms  # a single article is not a trend
    assert not is_discovery_term("EU AI Act") and is_discovery_term("Nvidia")
    assert discovery_key("GPT‑6 Astra") == "discover-gpt-6-astra"
    assert len(discovery_terms(top * 5, min_articles=1, limit=2)) == 2


def test_keywords_handle_possessives():
    assert "deepmind" in keywords("Import AI 472: DeepMind’s cheating models")


def test_simhash_near_duplicates():
    a = simhash("Meta Failed to Catch Hundreds of AI Child Abuse Ads")
    b = simhash("Meta failed to catch hundreds of AI child abuse ads, report says")
    c = simhash("Humanoid robots enter the warehouse")
    assert hamming(a, b) <= 10
    assert hamming(a, c) > 20


def test_slugify():
    assert slugify("IREN's partnership with NVIDIA expands…") == "iren-s-partnership-with-nvidia-expands"


def test_scrub_removes_sidebar_noise():
    md = "\n\n".join([
        "The deal will use Blackwell systems across 60MW in Texas.",
        "Investors will watch deployment timelines closely.",
        "More detail about the contract follows in the company filing, which runs to forty pages of dense text.",
        "Sponsored",
        "More for You",
        "Moneywise·19h",
        "Dow futures fall as Trump warns Iran amid market uncertainty.",
        "Jon Jones seeks UFC exit to face former heavyweight champion.",
    ])
    cleaned, notes = scrub(md)
    assert "UFC" not in cleaned and "Sponsored" not in cleaned and "Moneywise" not in cleaned
    assert "Blackwell" in cleaned
    assert any("cut at" in n for n in notes)


def test_validate_flags_mismatch():
    assert validate("word " * 300, "Anthropic releases Claude model update", 250, 8000) is not None
    assert validate("Anthropic releases the Claude model update today. " * 60, "Anthropic releases Claude model update", 250, 8000) is None


def test_enrich_clean_survives_malformed_answers():
    from types import SimpleNamespace

    from digest.enrich import _clean

    row = SimpleNamespace(title="OpenAI ships a model")
    # The answer that crashed the step on 14 September: key_points as a number.
    out = _clean({"key_points": 3, "entities": ["OpenAI"], "importance": "high",
                  "funding": {"company": "X", "investors": "Sequoia"}}, row, "models")
    assert out["key_points"] == [] and out["entities"]["companies"] == [] and out["importance"] == 5
    assert out["funding"]["investors"] == ["Sequoia"]
    assert _clean(["not", "a", "dict"], row, None)["headline"] == "OpenAI ships a model"
    assert _clean({"key_points": "One point"}, row, None)["key_points"] == ["One point"]


def test_daily_history_survives_event_pruning():
    import tempfile
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine, insert

    from digest import db, history

    tmp = Path(tempfile.mkdtemp()) / "history.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    at = lambda days, hour=12: (now - timedelta(days=days)).replace(hour=hour, minute=0)  # noqa: E731

    with eng.begin() as conn:
        conn.execute(insert(db.stories), [
            {"slug": "s1", "headline": "A", "first_published_at": at(5), "updated_at": at(5), "status": "published"},
            {"slug": "s2", "headline": "B", "first_published_at": at(1), "updated_at": at(1), "status": "published"},
            {"slug": "s3", "headline": "C", "first_published_at": at(1), "updated_at": at(1), "status": "unpublished"},
        ])
        conn.execute(insert(db.articles), [
            {"url": f"https://x.test/{i}", "fetched_at": at(d), "created_at": at(d), "status": st}
            for i, (d, st) in enumerate([(5, "published"), (5, "rejected"), (1, "published"), (1, "new"), (1, "published")])
        ])
        conn.execute(insert(db.events), [
            {"type": t, "session": s, "story_id": sid, "value": v, "created_at": when}
            for t, s, sid, v, when in [
                ("view", "a", 1, 1, at(2)), ("view", "a", 2, 1, at(2)), ("view", "b", 1, 1, at(2)),
                ("dwell", "a", 1, 30, at(2)), ("dwell", "a", 1, 10, at(2, 13)), ("dwell", "b", 1, 20, at(2)),
                ("click_source", "a", 1, 1, at(2)), ("push_on", "b", None, 1, at(2)), ("listen", "b", None, 1, at(2, 23)),
            ]
        ])
        conn.execute(insert(db.social_posts), [{"network": "bluesky", "kind": "story", "key": "s2", "created_at": at(1)}])
        conn.execute(insert(db.runs), [
            {"step": "fetch", "started_at": at(3), "stats": {"inserted": 3}},
            {"step": "enrich", "started_at": at(1), "stats": {"crashed": True}},
        ])

    google = {(now - timedelta(days=d)).date().isoformat(): (d, d * 10) for d in range(2, 9)}
    rows = {r["day"]: r for r in history.update(eng, now, google=google)}
    d = lambda n: (now - timedelta(days=n)).date().isoformat()  # noqa: E731

    assert min(rows) == d(8) and max(rows) == d(0) and len(rows) == 9  # from the first Search Console day to today
    two = rows[d(2)]
    assert (two["sessions"], two["views"], two["clicks"], two["alertSignups"], two["listens"]) == (2, 3, 1, 1, 1)
    assert two["dwellSeconds"] == 60 and two["dwellReads"] == 2  # 30s average per reading session
    assert rows[d(3)]["sessions"] is None  # before the first event: not measured, not zero
    assert rows[d(1)]["sessions"] == 0  # tracking live, nobody came
    assert (rows[d(5)]["articlesFetched"], rows[d(5)]["articlesPublished"], rows[d(5)]["storiesPublished"]) == (2, 1, 1)
    assert (rows[d(1)]["articlesFetched"], rows[d(1)]["articlesPublished"], rows[d(1)]["storiesPublished"]) == (3, 2, 1)
    assert rows[d(6)]["articlesFetched"] is None and rows[d(8)]["articlesFetched"] is None
    assert rows[d(2)]["socialPosts"] is None and rows[d(1)]["socialPosts"] == 1
    assert rows[d(1)]["crashedSteps"] == 1 and rows[d(2)]["crashedSteps"] == 0
    assert (rows[d(8)]["googleClicks"], rows[d(8)]["googleImpressions"]) == (8, 80)
    assert rows[d(1)]["googleClicks"] is None  # Search Console lag: unknown

    # Events get pruned, a late event lands yesterday, Search Console revises an old day.
    with eng.begin() as conn:
        conn.execute(db.events.delete())
        conn.execute(insert(db.events), [{"type": "view", "session": "c", "value": 1, "created_at": at(1, 20)}])
    google[d(7)] = (70, 700)
    later = now + timedelta(hours=1)
    rows = {r["day"]: r for r in history.update(eng, later, google=google)}
    assert len(rows) == 9
    assert rows[d(2)]["views"] == 3 and rows[d(2)]["dwellSeconds"] == 60  # kept after pruning
    assert rows[d(1)]["views"] == 1  # recomputed with the late event
    assert rows[d(7)]["googleClicks"] == 70


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", name, exc)
    sys.exit(1 if failures else 0)
