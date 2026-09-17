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


def test_enrich_time_budget_holds():
    import time as _t

    from digest import config, enrich

    assert enrich.should_start_article(0, [], 600, 90)
    assert not enrich.should_start_article(520, [], 600, 90)  # the first article's estimate would overrun
    assert enrich.should_start_article(500, [30, 40], 600, 90)
    assert not enrich.should_start_article(500, [30, 200], 600, 90)  # one stall raises the estimate
    enrich._deadline = _t.monotonic() + 12
    try:
        assert 5.0 <= enrich._request_timeout(90) <= 12.0
        assert enrich._request_timeout(3) == 5.0  # never below a workable floor
    finally:
        enrich._deadline = None
    assert enrich._request_timeout(90) == 90

    # A 25-second rate-limit window on every Groq model: skip to the error at once, never sleep.
    saved = dict(enrich._groq_wait_until)
    for m in [config.GROQ_MODEL, *config.GROQ_FALLBACK_MODELS]:
        enrich._groq_wait_until[m] = _t.time() + 25
    orig_post = enrich.requests.post

    def no_request(*a, **k):
        raise AssertionError("no request expected while rate limited")

    enrich.requests.post = no_request
    t0 = _t.monotonic()
    try:
        enrich.call_groq("prompt")
        raise AssertionError("expected a rate-limit error")
    except RuntimeError as exc:
        assert "rate limited" in str(exc)
    finally:
        enrich.requests.post = orig_post
        enrich._groq_wait_until.clear()
        enrich._groq_wait_until.update(saved)
    assert _t.monotonic() - t0 < 1.0


def test_fetch_all_parallel_hosts_keep_order_and_delays():
    from types import SimpleNamespace

    from digest.fetch import fetch_all

    srcs = [SimpleNamespace(key=f"s{i}", kind="rss", url=u) for i, u in enumerate([
        "https://www.bing.com/news/search?q=a", "https://techcrunch.com/feed", "https://www.bing.com/news/search?q=b",
        "https://www.reddit.com/r/x.rss", "https://www.reddit.com/r/y.rss", "https://bad.example/feed"])]
    slept = []

    def rss(source):
        if "bad" in source.url:
            raise ValueError("boom")
        return [source.key]

    out = fetch_all(srcs, fetchers={"rss": rss}, sleep=slept.append)
    assert [s.key for s, _, _ in out] == [s.key for s in srcs]
    assert out[5][1] is None and isinstance(out[5][2], ValueError) and out[0][1] == ["s0"]
    assert sorted(slept) == [1.0, 4.0]  # one gap on bing.com, a longer one on reddit.com


def _unit(v):
    import numpy as np

    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_cluster_merge_rule_resists_drift_and_caps():
    from digest.cluster import pick_story, split_members

    lead = _unit([1, 0, 0, 0])
    drifted_mean = _unit([1, 1, 0, 0])  # a big story's mean, pulled toward the generic topic
    stories = {1: {"vec": drifted_mean, "lead_vec": lead, "count": 30}}
    on_topic_only = _unit([0.55, 1, 0, 0])  # 0.96 to the mean, 0.48 to the lead
    assert pick_story(on_topic_only, stories, 0.82, 0.79) == (None, -1.0)
    same_event = _unit([1, 0.3, 0, 0])  # 0.88 to the mean, 0.96 to the lead
    sid, sim = pick_story(same_event, stories, 0.82, 0.79)
    assert sid == 1 and 0.82 <= sim < 0.9
    stories[1]["count"] = stories[1]["shown"] = 40
    assert pick_story(same_event, stories, 0.82, 0.79)[0] == 1  # full: still the same story (as overflow), never a second one
    assert pick_story(same_event, {2: {"vec": drifted_mean, "lead_vec": None, "count": 3}}, 0.82, 0.79)[0] == 2

    members = ([(1, lead)] + [(10 + i, _unit([1, 0.1 * i, 0, 0])) for i in range(5)]
               + [(20 + i, _unit([0.2, 0, 1, 0.1 * i])) for i in range(3)] + [(30, None)])
    keep, overflow, detach = split_members(1, lead, members, 0.79, 4)
    assert keep == [1, 10, 11, 12]
    assert overflow == [13, 14, 30]  # close to the lead but beyond the cap: counted, not shown
    assert detach == [22, 21, 20]  # far from the lead, least similar first


def test_cluster_full_story_takes_overflow_instead_of_a_second_story():
    import tempfile
    from datetime import datetime, timedelta, timezone

    import numpy as np
    from sqlalchemy import create_engine, insert, select

    from digest import cluster, config, db

    tmp = Path(tempfile.mkdtemp()) / "overflow.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    now = datetime.now(timezone.utc)
    rng = np.random.default_rng(1)

    def vec(base):
        v = np.asarray(base, dtype=np.float32) + 0.03 * rng.standard_normal(8).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    event, other = [1, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 1, 0]
    cap = config.CLUSTER_MAX_ARTICLES
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="s", name="S", url="https://s.test/feed"))
        # A story already at the cap, and three new articles: two on the same event, one on another.
        conn.execute(insert(db.stories).values(
            id=1, slug="mistral-raises", headline="Mistral raises 3B", lead_article_id=1, article_count=cap, importance=8,
            embedding=db.pack_vec(vec(event)), status="published", first_published_at=now - timedelta(hours=2), updated_at=now))
        conn.execute(insert(db.articles), [
            {"id": i, "url": f"https://s.test/{i}", "source_id": 1, "story_id": 1, "slug": f"a-{i}", "title": f"Article {i}",
             "headline": f"Article {i}", "importance": 8 if i == 1 else 5, "embedding": vec(event), "status": "published",
             "content_type": "news", "published_at": now - timedelta(hours=1), "fetched_at": now, "created_at": now}
            for i in range(1, cap + 1)])
        conn.execute(insert(db.articles), [
            {"id": 100 + i, "url": f"https://s.test/new{i}", "source_id": 1, "title": f"More on Mistral {i}", "headline": f"More on Mistral {i}",
             "importance": 9, "embedding": vec(event if i < 2 else other), "status": "enriched", "content_type": "news",
             "published_at": now, "fetched_at": now, "created_at": now} for i in range(3)])

    saved = (db._engine, cluster._MODEL, cluster._MODEL_NAME)
    db._engine, cluster._MODEL, cluster._MODEL_NAME = eng, False, "fallback-hash"
    try:
        stats = cluster.run()
    finally:
        db._engine, cluster._MODEL, cluster._MODEL_NAME = saved
    assert (stats["overflow"], stats["merged"], stats["new_stories"]) == (2, 0, 1), stats
    with eng.connect() as conn:
        story = conn.execute(select(db.stories).where(db.stories.c.id == 1)).one()
        arts = {a.id: a for a in conn.execute(select(db.articles)).all()}
    assert story.article_count == cap + 2 and story.headline == "Mistral raises 3B" and story.lead_article_id == 1
    assert arts[100].story_id == 1 and arts[100].status == "overflow" and arts[101].status == "overflow"
    assert arts[102].story_id not in (None, 1) and arts[102].status == "published"


def test_lead_rules_keep_news_over_commentary():
    from digest.merge import better_lead, opinion_headline, pick_lead

    crowdstrike = {"headline": "CrowdStrike buys an AI security startup", "content_type": "news", "importance": 6, "primary": False}
    amodei = {"headline": "Amodei urges AI slowdown as models outpace safety work", "content_type": "opinion", "importance": 9, "primary": False}
    assert not better_lead(amodei, crowdstrike)  # commentary never displaces news, however important
    assert better_lead(crowdstrike, amodei)  # news always displaces commentary
    assert not better_lead({**crowdstrike, "importance": 7}, crowdstrike)  # one point is not clearly better
    assert better_lead({**crowdstrike, "importance": 8}, crowdstrike)
    assert better_lead({**crowdstrike, "primary": True}, crowdstrike)  # a primary source at least as important leads
    assert not better_lead({**crowdstrike, "primary": True, "importance": 5}, crowdstrike)
    for h in ("Is the AI bubble about to burst?", "Why I stopped using Copilot", "How Mistral raised 3B in a week", "What the EU AI Act means for you"):
        assert opinion_headline(h), h
        assert not better_lead({**crowdstrike, "headline": h, "importance": 10}, crowdstrike), h
    assert not opinion_headline("A.I. startup raises $50M") and not opinion_headline("OpenAI ships GPT-6")
    assert better_lead({**amodei, "importance": 9}, {**amodei, "importance": 7})  # among commentary only: the margin rule

    from types import SimpleNamespace as NS
    members = [NS(id=1, status="published", content_type="opinion", importance=9, domain="blog.test", source_id=3),
               NS(id=2, status="published", content_type="news", importance=7, domain="press.test", source_id=3),
               NS(id=3, status="published", content_type="news", importance=7, domain="mistral.ai", source_id=1),
               NS(id=4, status="overflow", content_type="news", importance=10, domain="press.test", source_id=3)]
    texts = {1: NS(title="Why Mistral's round matters", headline="Why Mistral's round matters"),
             2: NS(title="Mistral raises 3B at 12B valuation", headline="Mistral raises 3B at 12B valuation"),
             3: NS(title="Announcing our Series C", headline="Mistral announces Series C"),
             4: NS(title="Mistral raises 3B", headline="Mistral raises 3B")}
    primary = lambda m: m.domain == "mistral.ai"  # noqa: E731
    assert pick_lead(members, texts, primary).id == 3  # news, primary and wording shared with the others
    assert pick_lead(members, texts, lambda m: False).id == 2  # without the primary point: equal, the earlier one


def test_cluster_repairs_oversized_story_and_reclusters():
    import tempfile
    from datetime import datetime, timedelta, timezone

    import numpy as np
    from sqlalchemy import create_engine, insert, select

    from digest import cluster, db

    tmp = Path(tempfile.mkdtemp()) / "cluster.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    now = datetime.now(timezone.utc)
    rng = np.random.default_rng(0)

    def vec(base):
        v = np.asarray(base, dtype=np.float32) + 0.05 * rng.standard_normal(8).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    event, topic = [1, 0, 0, 0, 0, 0, 0, 0], [0.3, 0, 0, 0, 1, 0, 0, 0]
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="s", name="S", url="https://s.test/feed"))
        conn.execute(insert(db.stories).values(
            id=1, slug="mistral", headline="Mistral", lead_article_id=1, article_count=50, importance=8,
            embedding=vec([0.8, 0, 0, 0, 0.6, 0, 0, 0]), status="published",
            first_published_at=now - timedelta(hours=2), updated_at=now))
        conn.execute(insert(db.articles), [
            {"id": i, "url": f"https://s.test/{i}", "source_id": 1, "story_id": 1, "slug": f"a-{i}",
             "title": f"Article {i}", "headline": f"Article {i}", "importance": 8 if i == 1 else 5,
             "embedding": vec(event if i <= 30 else topic), "status": "published",
             "published_at": now - timedelta(hours=1), "fetched_at": now, "created_at": now}
            for i in range(1, 51)])

    saved = (db._engine, cluster._MODEL, cluster._MODEL_NAME)
    db._engine, cluster._MODEL, cluster._MODEL_NAME = eng, False, "fallback-hash"  # no model download
    try:
        stats = cluster.run()
    finally:
        db._engine, cluster._MODEL, cluster._MODEL_NAME = saved
    assert (stats["stories_repaired"], stats["articles_detached"]) == (1, 20), stats
    assert (stats["embedded"], stats["new_stories"], stats["merged"]) == (0, 1, 19), stats
    with eng.connect() as conn:
        assert conn.execute(select(db.stories.c.article_count).where(db.stories.c.id == 1)).scalar() == 30
        arts = conn.execute(select(db.articles)).all()
    assert all(a.status == "published" for a in arts)
    assert {a.story_id for a in arts if a.id <= 30} == {1}
    assert len({a.story_id for a in arts if a.id > 30} - {1}) == 1 and all(a.story_id != 1 for a in arts if a.id > 30)
    assert all(a.slug == f"a-{a.id}" for a in arts)  # re-clustered articles keep their slugs


def test_title_year_marks_old_reposts():
    from types import SimpleNamespace

    from digest.gate import check
    from digest.textutil import strip_title_year, title_year

    assert title_year("Better language models and their implications: GPT2 will not be released (2019)") == 2019
    assert title_year("Attention is all you need [pdf] (2017)") == 2017
    assert title_year("Some paper [2021] [video]") == 2021
    assert title_year("GPT-5 (Part 2)") is None and title_year("The 2019 plan, revisited") is None
    assert strip_title_year("GPT2 will not be released (2019)") == "GPT2 will not be released"
    assert strip_title_year("Attention is all you need [pdf] (2017)") == "Attention is all you need [pdf]"
    assert clean_title("OpenAI restricts GPT-2 release over malicious use concerns (2019)") == \
        "OpenAI restricts GPT-2 release over malicious use concerns"

    text = ("OpenAI said it would not release the full GPT-2 language model because of concerns about "
            "malicious use of the AI system, releasing a smaller model to researchers instead. ") * 8
    row = SimpleNamespace(id=1, domain="openai.com", title="Better language models and their implications",
                          raw_title="Better language models and their implications (2019)", content_text=text,
                          description=None, published_at=None, simhash=None)
    assert check(row, []) == "too old (title says 2019)"
    assert check(SimpleNamespace(**{**vars(row), "raw_title": row.title}), []) is None


def test_page_date_replaces_recent_submission_time():
    from datetime import datetime, timezone

    from digest.extract import extract, parse_page_date, prefer_page_date

    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    hn_time = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)
    old = prefer_page_date(hn_time, "2019-02-14T08:00:00-08:00", now=now, min_gap_days=3)
    assert old == datetime(2019, 2, 14, 16, 0, tzinfo=timezone.utc)
    assert prefer_page_date(hn_time, "2026-09-12T09:00:00Z", now=now, min_gap_days=3) is None  # within the gap
    assert prefer_page_date(None, "2026-09-12", now=now) is not None  # no feed date: use the page's
    assert prefer_page_date(hn_time, "2030-01-01", now=now) is None  # implausible future
    assert parse_page_date("Feb 14, 2019") is None and parse_page_date(None) is None

    body = " ".join(["OpenAI decided not to release the full GPT-2 language model over misuse concerns."] * 40)
    for head in ('<meta property="article:published_time" content="2019-02-14T08:00:00Z">',
                 '<meta property="og:published_time" content="2019-02-14T08:00:00Z">', ""):
        time_tag = "" if head else '<time datetime="2019-02-14T08:00:00Z">Feb 14, 2019</time>'
        html = (f"<html><head><title>Better language models</title>{head}</head><body><article><header>{time_tag}"
                f"<h1>Better language models</h1></header><p>{body}</p></article></body></html>")
        res = extract("https://openai.com/blog/better-language-models", html, "OpenAI GPT-2 language model release")
        assert parse_page_date(res.date) == datetime(2019, 2, 14, 8, 0, tzinfo=timezone.utc), (head, res.date)


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
    assert two["visitors"] == two["sessions"]  # events without a visitor number count one per session
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


def test_source_names_for_social_apps():
    from digest.admin import source_name

    cases = {"l.instagram.com": "Instagram", "instagram.com": "Instagram", "www.instagram.com": "Instagram", "instagram": "Instagram",
             "com.instagram.android": "Instagram", "threads.net": "Threads", "l.threads.com": "Threads", "m.youtube.com": "YouTube",
             "youtu.be": "YouTube", "tiktok.com": "TikTok", "mastodon.social": "Mastodon", "fosstodon.org": "Mastodon",
             "mastodon.example.org": "Mastodon", "t.me": "Telegram", "web.telegram.org": "Telegram", "discord.com": "Discord",
             "app.slack.com": "Slack", "web.whatsapp.com": "WhatsApp", "wa.me": "WhatsApp", "com.linkedin.android": "LinkedIn",
             "t.co": "X", "l.facebook.com": "Facebook", "google.co.uk": "Google", "direct": "Direct", "example.com": "example.com",
             "notinstagram.com": "notinstagram.com"}
    for raw, name in cases.items():
        assert source_name(raw) == name, (raw, source_name(raw))


def test_events_policy_and_guard_allow_new_reader_events():
    from digest import db

    policy = "\n".join(db.EVENTS_POLICY_SQL)
    for t in ("view", "dwell", "search", "depth"):
        assert f"'{t}'" in policy
    assert "DROP POLICY IF EXISTS" in policy and "'full_text'" in policy and "value <= 100" in policy
    assert "length(coalesce(new.detail, '')) > 100" in db.EVENTS_GUARD_SQL and "regexp_replace" in db.EVENTS_GUARD_SQL
    schema = (Path(__file__).resolve().parents[2] / "supabase" / "schema.sql").read_text(encoding="utf-8")
    assert "'listen', 'search', 'depth')" in schema and "length(coalesce(new.detail, '')) > 100" in schema
    assert db.events.c.detail.type.length == 100


def test_search_and_depth_summaries():
    from datetime import datetime, timedelta, timezone

    from digest import admin

    t0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    rows = [("GPT-6 ", 12, "v1", t0), ("gpt-6", 12, "v2", t0 + timedelta(hours=1)), ("mistral agents", 3, "v1", t0),
            ("robot dogs", 2, "v1", t0), ("robot dogs", 0, "v3", t0 + timedelta(hours=2)),  # the latest search found nothing
            ("x", 0, "v4", t0), ("mail me at a@b.co 1234567", 0, "v5", t0)]
    s = admin.search_summary(rows)
    assert s["total"] == 6 and s["queries"] == 4
    assert s["top"][0] == {"query": "gpt-6", "searches": 2, "visitors": 2, "results": 12}
    assert [m["query"] for m in s["missing"]] == ["robot dogs", "mail me at"]
    assert s["noResults"] == 3
    assert admin.clean_query("  Call 0612345678 or me@x.org  ") == "call or"

    d = admin.depth_summary([("a", 1, 10, "summary"), ("a", 1, 80, "full_text"), ("b", 1, 0, "top"),
                             ("c", 2, 100, "end"), ("c", 2, 40, "summary"), ("d", 2, 250, "bogus")])
    assert d["reads"] == 4 and d["stages"] == {"top": 2, "summary": 0, "full_text": 1, "end": 1}
    assert d["perStory"] == {1: 40, 2: 100} and d["avgPercent"] == 70


def test_daily_history_counts_searches():
    import tempfile
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine, insert

    from digest import db, history

    tmp = Path(tempfile.mkdtemp()) / "searches.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    y = now - timedelta(days=1)
    with eng.begin() as conn:
        conn.execute(insert(db.events), [
            {"type": "view", "session": "a", "value": 1, "created_at": y},
            {"type": "search", "session": "a", "value": 0, "detail": "robot dogs", "created_at": y},
            {"type": "search", "session": "a", "value": 4, "detail": "gemini", "created_at": y},
            {"type": "depth", "session": "a", "story_id": 1, "value": 60, "detail": "full_text", "created_at": y},
        ])
    rows = {r["day"]: r for r in history.update(eng, now, google={})}
    assert rows[y.date().isoformat()]["searches"] == 2 and rows[now.date().isoformat()]["searches"] == 0
    assert rows[y.date().isoformat()]["views"] == 1


def _gsc_row(keys, clicks, impressions, position):
    return {"keys": keys, "clicks": clicks, "impressions": impressions, "ctr": clicks / impressions if impressions else 0, "position": position}


def test_gsc_build_fills_gaps_and_weights_position():
    from digest import gsc

    by_day = [_gsc_row(["2026-09-01"], 0, 1, 80.0), _gsc_row(["2026-09-03"], 1, 9, 5.0)]
    by_day_query = [_gsc_row(["2026-09-01", "ai digest"], 0, 1, 80.0), _gsc_row(["2026-09-03", "ai digest"], 1, 6, 4.0),
                    _gsc_row(["2026-09-03", "digest ai"], 0, 3, 7.0)]
    queries = [_gsc_row(["digest ai"], 0, 3, 7.0), _gsc_row(["ai digest"], 1, 7, 14.9)]
    prev_queries = [_gsc_row(["ai digest"], 0, 2, 30.0)]
    pages = [_gsc_row(["https://digestai.news/"], 1, 10, 12.5), _gsc_row(["https://digestai.news/today"], 0, 0, 0)]
    out = gsc.build(by_day, by_day_query, queries, prev_queries, pages, [], "2026-09-02", "2026-09-04")

    assert [r["day"] for r in out["history"]] == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert [r["day"] for r in out["perDay"]] == ["2026-09-02", "2026-09-03", "2026-09-04"]
    gap = out["perDay"][0]
    assert gap["impressions"] == 0 and gap["position"] is None and gap["queries"] == 0  # a gap, never position 0
    assert out["perDay"][1]["queries"] == 2 and "queries" not in out["history"][0]  # outside the date x query window
    # Weighted: the 9-impression day at 5 outweighs nothing else in the window; 80 is before it.
    assert out["totals"] == {"clicks": 1, "impressions": 9, "position": 5.0}
    assert gsc.weighted_position([{"impressions": 1, "position": 80}, {"impressions": 9, "position": 5}, {"impressions": 0, "position": None}]) == 12.5
    top = out["queries"][0]
    assert top["query"] == "ai digest" and top["prevPosition"] == 30.0 and top["days"] == [["2026-09-01", 1, 80.0], ["2026-09-03", 6, 4.0]]
    assert "prevPosition" not in out["queries"][1] and out["queries"][1]["days"] == [["2026-09-03", 3, 7.0]]
    assert out["pages"][0]["page"] == "/" and out["pageCount"] == 1 and out["queryCount"] == 2 and out["prevQueryCount"] == 1


def test_gsc_run_with_fake_session():
    import json
    import os
    import tempfile

    from digest import config, gsc

    calls = []

    class Resp:
        def __init__(self, rows=None, ok=True):
            self.status_code, self.ok, self._rows = (200 if ok else 404), ok, rows

        def json(self):
            return {"rows": self._rows or []}

    class Session:
        headers: dict = {}

        def post(self, url, json=None, timeout=None):  # noqa: A002
            calls.append(json)
            dims = json["dimensions"]
            end = json["endDate"]
            if dims == ["date"]:
                return Resp([_gsc_row([end], 0, 4, 12.0)])
            if dims == ["date", "query"]:
                return Resp([_gsc_row([end, "ai news"], 0, 4, 12.0)])
            return Resp([_gsc_row(["ai news" if dims == ["query"] else "https://digestai.news/"], 0, 4, 12.0)])

        def get(self, url, timeout=None):
            return Resp(ok=False)

    tmp = Path(tempfile.mkdtemp())
    saved = (config.SITE_DATA_DIR, gsc._token, gsc.requests.Session, gsc._inspections, os.environ.get("GSC_SERVICE_ACCOUNT_JSON"), os.environ.get("GSC_PROPERTY"))
    try:
        config.SITE_DATA_DIR, gsc._token, gsc.requests.Session, gsc._inspections = tmp, (lambda sa: "t"), Session, (lambda s, p: [])
        os.environ["GSC_SERVICE_ACCOUNT_JSON"], os.environ["GSC_PROPERTY"] = "{}", "https://digestai.news/"
        stats = gsc.run()
    finally:
        config.SITE_DATA_DIR, gsc._token, gsc.requests.Session, gsc._inspections = saved[:4]
        for k, v in zip(("GSC_SERVICE_ACCOUNT_JSON", "GSC_PROPERTY"), saved[4:]):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert "error" not in stats, stats
    data = json.loads((tmp / "gsc.json").read_text(encoding="utf-8"))
    assert [c["dimensions"] for c in calls] == [["date"], ["date", "query"], ["query"], ["query"], ["page"], ["page"]]
    assert all(c["rowLimit"] <= 25000 for c in calls)
    assert calls[3]["endDate"] < data["start"] == calls[2]["startDate"]  # previous window ends before this one
    assert len(data["perDay"]) == 28 and data["perDay"][-1]["position"] == 12.0 and data["perDay"][0]["position"] is None
    assert data["queries"][0]["days"] == [[data["end"], 4, 12.0]] and data["queries"][0]["prevPosition"] == 12.0
    assert stats["position"] == 12.0 and stats["queries"] == 1


def test_daily_history_google_position_weighting():
    import tempfile
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine

    from digest import db, history

    assert history.google_values((3, 0, None, None)) == {"google_clicks": 3, "google_impressions": 0, "google_position": None,
                                                         "google_position_sum": 0.0, "google_queries": 0}
    assert history.google_values((0, 4, 12.5, 2))["google_position_sum"] == 50.0
    kept = {"google_impressions": 4, "google_position": 12.5, "google_position_sum": 50.0, "google_queries": 2}
    assert history.google_values((0, 4), kept) == {"google_clicks": 0, "google_impressions": 4, **{k: kept[k] for k in kept if k != "google_impressions"}}
    assert history.google_values((0, 5), kept)["google_position"] is None  # impressions changed: the old position is stale

    tmp = Path(tempfile.mkdtemp()) / "history.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    d = lambda n: (now - timedelta(days=n)).date().isoformat()  # noqa: E731
    # An older run stored clicks and impressions only; this run knows positions and queries.
    # Like google_days: every day in Search Console's range, zero where it reported nothing.
    empty = {d(n): (0, 0, None, 0) for n in range(3, 21)}
    history.update(eng, now, google={**empty, d(20): (0, 1), d(3): (0, 9)})
    rows = {r["day"]: r for r in history.update(eng, now + timedelta(hours=1), google={**empty, d(20): (0, 1, 80.0, None), d(3): (1, 9, 5.0, 3)})}
    assert rows[d(20)]["googlePosition"] == 80.0 and rows[d(20)]["googlePositionSum"] == 80.0  # backfilled on an old day
    assert rows[d(3)]["googlePositionSum"] == 45.0 and rows[d(3)]["googleQueries"] == 3
    assert rows[d(10)]["googlePosition"] is None and rows[d(10)]["googlePositionSum"] == 0.0  # no impressions: a gap
    sum_pos = sum(r["googlePositionSum"] or 0 for r in rows.values())
    sum_imp = sum(r["googleImpressions"] or 0 for r in rows.values())
    assert sum_pos / sum_imp == 12.5  # (80x1 + 5x9) / 10, not the mean of daily averages (42.5)


def _compare_js():
    """The Compare script from admin.astro, runnable in Node (it exports itself without a DOM)."""
    import re
    import shutil

    node = shutil.which("node")
    if not node:
        return None, None
    page = (Path(__file__).resolve().parents[2] / "site" / "src" / "pages" / "admin.astro").read_text(encoding="utf-8")
    script = next(s for s in re.findall(r"<script is:inline>(.*?)</script>", page, re.S) if "digestCompare" in s)
    return node, script


def test_compare_average_position_is_impression_weighted():
    import json
    import subprocess

    node, script = _compare_js()
    if not node:
        print("SKIP node not installed")
        return
    day = lambda n: f"2026-09-{n:02d}"  # noqa: E731
    history = []
    for n in range(1, 15):
        imp, pos = (0, None) if n in (3, 10) else ((90, 5.0) if n % 2 else (10, 50.0))
        if n >= 8:  # the latest week ranks better on its busy days
            pos = pos and pos - 2
        history.append({"day": day(n), "googleClicks": 1 if imp else 0, "googleImpressions": imp, "googlePosition": pos,
                        "googlePositionSum": (pos or 0) * imp})
    probe = script + "\nconst c = globalThis.digestCompare;\n" + (
        f"const h = {json.dumps(history)};\n"
        "const r = c.compare(h, ['2026-09-08','2026-09-14'], ['2026-09-01','2026-09-07']);\n"
        "console.log(JSON.stringify(r.filter(x => x.key.startsWith('google'))));")
    res = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    got = {r["key"]: r for r in json.loads(res.stdout)}
    pos = got["googlePosition"]
    # Week A: days 8..14 minus day 10 -> 90x3 at 3, 10x3 at 48 -> (810 + 1440) / 300 = 7.5.
    # A mean of daily averages would give 25.5.
    assert abs(pos["a"] - 7.5) < 1e-9, pos
    assert abs(pos["b"] - 9.5) < 1e-9, pos  # (270x5 + 30x50) / 300
    assert pos["state"] == "diff" and pos["tone"] == "good"  # lower is better
    ctr = got["googleCtr"]
    assert abs(ctr["a"] - 6 / 300) < 1e-9 and ctr["state"] == "same"
    # Few impressions: shown, but not judged.
    thin = [{**r, "googleImpressions": 1, "googlePositionSum": r["googlePosition"] or 0} for r in history]
    probe = script + "\nconst c = globalThis.digestCompare;\n" + (
        f"const h = {json.dumps(thin)};\n"
        "const r = c.compare(h, ['2026-09-08','2026-09-14'], ['2026-09-01','2026-09-07']);\n"
        "console.log(JSON.stringify(r.find(x => x.key === 'googlePosition')));")
    res = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    small = json.loads(res.stdout)
    assert small["state"] == "small" and small["tone"] == "neutral", small


def test_briefing_keeps_a_place_for_the_focus_category():
    from datetime import datetime, timedelta, timezone

    from digest import config, export

    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")  # noqa: E731

    def story(i, score, category="models", hours=2):
        return {"id": i, "score": score, "pinned": False, "category": category, "firstPublishedAt": iso(hours),
                "articles": [{"publishedAt": iso(hours)}], "articleCount": 1, "summaryMd": "words " * 50}

    stories = [story(i, 0.9 - i * 0.01) for i in range(1, 20)] + [story(99, 0.2, "marketing")]
    top = export.build_briefing(stories, now)["storyIds"]
    assert len(top) == export.BRIEFING_SIZE and top[-1] == 99 and 1 in top
    # Already in the top on score: nothing moves. No fresh focus story: nothing is forced in.
    assert export.build_briefing([story(1, 0.95, "marketing")] + stories[:-1], now)["storyIds"][0] == 1
    stale = stories[:-1] + [story(99, 0.2, "marketing", hours=200)]
    assert 99 not in export.build_briefing(stale, now)["storyIds"]
    assert config.FOCUS_CATEGORIES.get("marketing", 0) > 0 and list(config.CATEGORIES)[1] == "marketing"


def test_topic_intro_grounding():
    from digest.topics import PROMPT, grounded_description, story_lines, templated_intro, unsupported_names

    stories = [
        {"headline": "Nvidia releases Nemotron 4 as open weights", "summaryMd": "Nvidia has published **Nemotron 4**, a 340B model, under an open licence.\n\nMore detail.",
         "keyPoints": ["340B parameters"], "firstPublishedAt": "2026-09-10T08:00:00Z"},
        {"headline": "Nemotron 4 tops the open-model leaderboard", "summaryMd": None, "keyPoints": ["Beats Llama 4 on MMLU"], "firstPublishedAt": "2026-09-12T08:00:00Z"},
        {"headline": "Hugging Face adds Nemotron 4 to its inference API", "summaryMd": "Hugging Face now serves the model.", "keyPoints": [], "firstPublishedAt": "2026-09-14T08:00:00Z"},
    ]
    evidence = story_lines(stories)
    # Newest first, digest under the headline, key points when there is no digest.
    assert evidence.startswith("- Hugging Face adds Nemotron 4") and "a 340B model" in evidence and "Beats Llama 4 on MMLU" in evidence
    assert "**" not in evidence and "More detail" not in evidence
    prompt = PROMPT.format(name="Nemotron", kind="AI model", stories=evidence)
    assert "Do not add anything you know from elsewhere" in prompt
    source = "Nemotron\n" + evidence

    # Facts the stories state pass; names they never mention (the intro that said Nemotron was
    # "developed by Palantir") do not. Sentence-starting common words and digits are not names.
    assert unsupported_names("Nemotron 4 is Nvidia's open-weights model. It tops the leaderboard and Hugging Face serves it.", source) == []
    assert unsupported_names("Nemotron is a model family developed by Palantir. Recently it beat Llama 4.", source) == ["Palantir"]
    assert unsupported_names("Qwen is a Chinese AI research firm within DAMO Academy.", "Qwen\n- Qwen 3 released") == ["Chinese", "DAMO", "Academy"]
    assert unsupported_names("The model, GPT-7, ships in 2027.", source) == ["GPT-7", "2027"]

    line = templated_intro(stories)
    assert line == "3 stories since 10 September 2026, most recently: Hugging Face adds Nemotron 4 to its inference API."
    import re
    assert re.match(r"^\d+ stor(y|ies) since ", line)  # the site keeps this out of the "What is X?" answer

    good = "Nemotron 4 is Nvidia's open-weights model with 340B parameters. It tops the open-model leaderboard and Hugging Face serves it through its inference API."
    assert grounded_description("Nemotron", "AI model", stories, lambda p: {"description": good}) == (good, True)
    bad = "Nemotron is developed by Palantir and Nvidia and competes with Llama 4 on the open-model leaderboard."
    assert grounded_description("Nemotron", "AI model", stories, lambda p: {"description": bad}) == (line, False)
    assert grounded_description("Nemotron", "AI model", stories, lambda p: {"description": "Too short."}) == (line, False)

    def boom(p):
        raise RuntimeError("quota")
    assert grounded_description("Nemotron", "AI model", stories, boom) == (line, False)


# The FT's subscription wall as the extractor saw it for the Houthi missiles story (16 Sep 2026).
FT_TEASER = ("Houthis use Anthropic AI to develop ballistic missiles\n\n"
             "was undefined now undefined\n\n"
             "Subscribe to unlock this article. Try unlimited access. Only $1 for 4 weeks. Then $75 per month. "
             "New customers only. Cancel anytime during your trial.\n\n"
             "Complete digital access to quality FT journalism on any device. Pay a year upfront and get 20% off. "
             "Explore more offers. Standard Digital: Essential digital access to quality FT journalism. "
             "Premium Digital: Complete digital access with expert analysis. "
             "Already a subscriber? Sign in to read the article. Check whether you already have access via your university or organisation. "
             "Terms and conditions apply. Prices shown are in US dollars.")
ARTICLE_300 = ("The lab said on Monday that its new model handles longer documents and answers questions about them. " * 15
               + "Subscribe to our newsletter for more stories like this one every morning.")


def test_teaser_reason_catches_paywall_pitches():
    from digest.extract import teaser_reason

    assert teaser_reason(FT_TEASER).startswith("paywall teaser"), teaser_reason(FT_TEASER)
    assert teaser_reason("undefined now undefined " + "word " * 100).startswith("paywall teaser")  # "undefined" alone is enough
    assert teaser_reason(ARTICLE_300) is None  # one newsletter line in a real article is not a wall
    assert teaser_reason("A short brief with ninety words. " * 18) is None  # short but honest
    assert teaser_reason("Only forty words. " * 12).startswith("too short")
    assert teaser_reason("Only the opening paragraph came through. " * 15, feed_words=600).startswith("far shorter")
    assert teaser_reason("Only the opening paragraph came through. " * 15, feed_words=100) is None


def test_extract_and_gate_treat_a_teaser_as_no_text():
    from types import SimpleNamespace

    from digest import extract as ex, gate

    saved = ex._extract

    def fake(url, html, title, feed_content=None, content_from_feed=False, min_words=None):
        return ex.Extraction(ok=True, method="feed" if content_from_feed else "trafilatura-short", markdown=FT_TEASER, text=FT_TEASER, words=110)

    ex._extract = fake
    try:
        res = ex.extract("https://ft.com/content/x", "<html></html>", "Houthis use Anthropic AI to develop ballistic missiles")
        assert not res.ok and res.teaser and res.reason.startswith("paywalled, no readable text"), res
        # A publisher whose feed carries the whole article is never a paywall.
        res = ex.extract("https://blog.test/x", None, "Title", feed_content=FT_TEASER, content_from_feed=True)
        assert res.ok and not res.teaser
    finally:
        ex._extract = saved
    row = SimpleNamespace(id=1, domain="ft.com", title="Houthis use Anthropic AI to develop ballistic missiles", raw_title=None,
                          published_at=None, simhash=None, content_text=FT_TEASER, description="")
    assert gate.check(row, []) == "paywalled, no readable text"
    row.description = "The Houthi movement has been using Anthropic's models to work on missile guidance, according to a report. " * 3
    assert gate.check(row, []) != "paywalled, no readable text"  # the feed description can be the digest


def test_headline_hedged_when_the_source_only_suggests():
    from digest.enrich import _clean, headline_hedged

    title = "Have You Protested AI Recently? Anthropic May Be Watching You"
    assert headline_hedged(title, "Anthropic builds predictive surveillance system to monitor AI critics")
    assert not headline_hedged(title, "Anthropic may be monitoring AI critics, report says")
    assert not headline_hedged(title, "Is Anthropic watching AI protesters?")
    assert not headline_hedged("Is the AI boom a bubble?", "Opinion: the AI boom looks like a bubble")
    assert headline_hedged("OpenAI could raise $40B, sources say", "OpenAI raises $40B")
    assert not headline_hedged("OpenAI could raise $40B, sources say", "OpenAI in talks to raise $40B")
    assert not headline_hedged("OpenAI raises $40B", "OpenAI closes $40B round")  # nothing hedged to lose
    # A company describing its own action is attribution, not a hedge (a false positive on the export check).
    assert not headline_hedged("Anthropic says Claude thwarted bioweapon research from state-sponsored actors",
                               "Anthropic Blocks State-Sponsored Actors Using Claude for Bioweapon Research")
    assert headline_hedged("Nvidia reportedly in talks to buy Groq", "Nvidia buys Groq")
    assert not headline_hedged("Nvidia reportedly in talks to buy Groq", "Nvidia in talks to buy Groq, report says")
    assert not headline_hedged(None, "x") and not headline_hedged(title, title)
    from types import SimpleNamespace

    row = SimpleNamespace(title=title)
    assert _clean({"headline": "Anthropic builds predictive surveillance system to monitor AI critics"}, row, None)["hedged"]
    assert not _clean({"headline": "Anthropic may be monitoring AI critics"}, row, None)["hedged"]


def _repair_db():
    """A temporary SQLite database with change tracking, as db.engine() sets it up."""
    import tempfile

    from sqlalchemy import create_engine

    from digest import db

    tmp = Path(tempfile.mkdtemp()) / "repair.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    assert db.install_change_tracking(eng)
    return eng


def test_repair_unwraps_bing_links_in_bounded_batches():
    from datetime import datetime, timezone

    from sqlalchemy import insert, select

    from digest import db, repair

    eng = _repair_db()
    now = datetime.now(timezone.utc)
    bing = lambda tid, target: f"https://bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid={tid}&url={target}&c=1"  # noqa: E731
    verge = "https%3a%2f%2fwww.theverge.com%2fai%2f123%2fopenai-model"
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="s", name="S", url="https://s.test/feed"))
        conn.execute(insert(db.stories).values(id=1, slug="s", headline="S", lead_article_id=2, first_published_at=now, updated_at=now))
        base = {"source_id": 1, "story_id": 1, "status": "published", "fetched_at": now, "created_at": now, "title": "OpenAI model"}
        conn.execute(insert(db.articles), [
            {**base, "id": 1, "url": "https://theverge.com/ai/123/openai-model", "domain": "theverge.com", "slug": "a1"},
            {**base, "id": 2, "url": bing("aa", verge), "domain": "bing.com", "slug": "a2"},  # already stored unwrapped as #1
            {**base, "id": 3, "url": bing("bb", "https%3a%2f%2fwww.wired.com%2fstory%2fx"), "domain": "bing.com", "slug": "a3"},
            {**base, "id": 4, "url": bing("cc", "https%3a%2f%2fwww.wired.com%2fstory%2fx%3futm_source%3dbing"), "domain": "bing.com", "slug": "a4"},
            {**base, "id": 5, "url": bing("dd", "javascript%3aalert(1)"), "domain": "bing.com", "slug": "a5"},
            {**base, "id": 6, "url": bing("ee", "https%3a%2f%2fwww.wired.com%2fstory%2fy"), "domain": "bing.com", "slug": "a6", "status": "rejected"},
        ])
        wm = db.watermark(conn)
    first = repair.bing_links(eng, limit=2)
    assert first == {"duplicates": 1, "unwrapped": 1}, first  # #2 duplicates #1, #3 becomes wired.com
    second = repair.bing_links(eng, limit=2)
    assert second == {"duplicates": 1, "unwrappable": 1}, second  # #4 duplicates #3 (same page, tracking stripped), #5 has no target
    assert repair.bing_links(eng) == {}  # done; the rejected #6 is left alone
    a = db.articles.c
    with eng.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(a.id, a.url, a.domain, a.status, a.reject_reason, a.rev)).all()}
    assert (rows[3].url, rows[3].domain, rows[3].status) == ("https://wired.com/story/x", "wired.com", "published")
    assert (rows[2].status, rows[2].reject_reason) == ("rejected", "repair: duplicate of #1")
    assert (rows[4].status, rows[4].reject_reason) == ("rejected", "repair: duplicate of #3")
    assert rows[5].status == "rejected" and "unwrapped" in rows[5].reject_reason
    assert rows[1].rev < wm and rows[6].url.startswith("https://bing.com")
    # The pages show links and domains, so every repaired row must look changed to the runner's copy.
    assert all(rows[i].rev >= wm for i in (2, 3, 4, 5)), {i: rows[i].rev for i in rows}


def test_repair_hides_teasers_and_unpublishes_bare_stories():
    from datetime import datetime, timezone

    from sqlalchemy import insert, select

    from digest import db, repair

    eng = _repair_db()
    now = datetime.now(timezone.utc)
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="s", name="S", url="https://s.test/feed"))
        conn.execute(insert(db.stories), [
            {"id": 1, "slug": "houthis", "headline": "Houthis use Anthropic AI", "lead_article_id": 1, "first_published_at": now, "updated_at": now},
            {"id": 2, "slug": "covered", "headline": "Covered elsewhere", "lead_article_id": 2, "first_published_at": now, "updated_at": now},
            {"id": 3, "slug": "described", "headline": "Feed description only", "lead_article_id": 4, "first_published_at": now, "updated_at": now},
        ])
        # executemany takes its columns from the first row: every row must name description.
        base = {"source_id": 1, "status": "published", "fetched_at": now, "created_at": now, "show_fulltext": True, "extraction_ok": True,
                "description": None}
        conn.execute(insert(db.articles), [
            {**base, "id": 1, "url": "https://ft.com/1", "story_id": 1, "content_md": FT_TEASER, "word_count": 110},
            {**base, "id": 2, "url": "https://ft.com/2", "story_id": 2, "content_md": FT_TEASER, "word_count": 110},
            {**base, "id": 3, "url": "https://wired.com/2", "story_id": 2, "content_md": ARTICLE_300, "word_count": 300},
            {**base, "id": 4, "url": "https://ft.com/3", "story_id": 3, "content_md": FT_TEASER, "word_count": 110},
            {**base, "id": 5, "url": "https://blog.test/3", "story_id": 3, "content_md": None, "word_count": 0, "show_fulltext": False,
             "description": "A feed description long enough to be the digest of this story on its own. " * 4},
            {**base, "id": 6, "url": "https://long.test/6", "story_id": 2, "content_md": "Subscribe to unlock " + ARTICLE_300 * 3, "word_count": 900},
        ])
        wm = db.watermark(conn)
    assert repair.teasers(eng, limit=2) == {"hidden": 2, "stories_unpublished": 1}  # #1 and #2; story 1 had nothing else
    assert repair.teasers(eng) == {"hidden": 1}  # #4; story 3 keeps its described article
    assert repair.teasers(eng) == {}
    a, s = db.articles.c, db.stories.c
    with eng.connect() as conn:
        arts = {r.id: r for r in conn.execute(select(a.id, a.show_fulltext, a.extraction_ok, a.status, a.rev)).all()}
        stories = dict(conn.execute(select(s.id, s.status)).all())
    assert all(not arts[i].show_fulltext and not arts[i].extraction_ok and arts[i].status == "published" for i in (1, 2, 4))
    assert arts[3].show_fulltext and arts[6].show_fulltext  # a long article that mentions a wall is an article
    assert stories == {1: "unpublished", 2: "published", 3: "published"}
    assert all(arts[i].rev >= wm for i in (1, 2, 4)) and arts[3].rev < wm


def test_ollama_cloud_skips_paid_models_and_pauses_on_limits():
    from digest import config, enrich

    calls = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, str(body)

        def json(self):
            return self._body

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append(json["model"])
        if json["model"] == "paid-model":
            return Resp(402, {"error": "this model requires a subscription"})
        if json["model"] == "busy-model":
            return Resp(429, {"error": "usage limit"})
        return Resp(200, {"message": {"content": '{"headline": "ok"}'}})

    saved = (enrich.requests.post, config.OLLAMA_CLOUD_MODEL, config.OLLAMA_CLOUD_FALLBACK_MODELS, set(enrich._cloud_dead))
    try:
        enrich.requests.post = fake_post
        enrich._cloud_dead.clear()
        config.OLLAMA_CLOUD_MODEL, config.OLLAMA_CLOUD_FALLBACK_MODELS = "paid-model", ["free-model"]
        assert enrich.call_ollama_cloud("prompt") == {"headline": "ok"}
        assert calls == ["paid-model", "free-model"] and "paid-model" in enrich._cloud_dead
        enrich.call_ollama_cloud("prompt")
        assert calls[-1] == "free-model" and calls.count("paid-model") == 1  # not asked again this run
        config.OLLAMA_CLOUD_MODEL, config.OLLAMA_CLOUD_FALLBACK_MODELS = "busy-model", []
        try:
            enrich.call_ollama_cloud("prompt")
            raise AssertionError("a usage limit must pause the provider")
        except enrich.ProviderPaused:
            pass
    finally:
        enrich.requests.post, config.OLLAMA_CLOUD_MODEL, config.OLLAMA_CLOUD_FALLBACK_MODELS = saved[:3]
        enrich._cloud_dead.clear()
        enrich._cloud_dead.update(saved[3])


def test_reader_countries_from_time_zones():
    from digest import admin

    assert admin.country_of("Europe/Warsaw") == "PL" and admin.country_of("America/New_York") == "US"
    assert admin.country_of("Asia/Calcutta") == "IN" and admin.country_of("Europe/Kiev") == "UA"  # older names
    assert admin.country_of("UTC") is None and admin.country_of(None) is None
    rows = [("Europe/Warsaw", 5, 3), ("America/Chicago", 4, 2), ("America/New_York", 2, 1), (None, 9, 6), ("Etc/GMT", 1, 1)]
    out = admin.countries_summary(rows)
    assert [(c["name"], c["visitors"], c["views"]) for c in out] == [("United States", 3, 6), ("Poland", 3, 5), ("Unknown", 7, 10)]


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
