"""Summary quality: which article the best model reads, what is checked before a summary is
stored, and the second pass that rewrites the stories that turned out to matter.

Offline, on a temporary SQLite database with fake providers: python tests/test_summaries.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, insert, select  # noqa: E402

from digest import admin, cache, checks, config, db, enrich, upgrade  # noqa: E402

NOW = datetime.now(timezone.utc)


@contextmanager
def fresh_db():
    """A temporary database with its own cache directory, as db.engine() would set it up."""
    tmp = Path(tempfile.mkdtemp())
    saved = (db._engine, config.CACHE_DIR, config.SITE_DATA_DIR)
    eng = create_engine(f"sqlite:///{(tmp / 'test.db').as_posix()}", future=True)
    db.install_read_meter(eng)
    db.metadata.create_all(eng)
    db.install_change_tracking(eng)
    db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = eng, tmp / "cache", tmp / "site"
    cache.reset()
    try:
        yield eng
    finally:
        db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = saved
        cache.reset()
        eng.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def patched(**values):
    """Module attributes set for the duration (config flags, provider functions)."""
    saved = [(obj, name, getattr(obj, name)) for obj, name in [(v[0], v[1]) for v in values.values()]]
    try:
        yield
    finally:
        for obj, name, old in saved:
            setattr(obj, name, old)


class Patch:
    """Set attributes on modules and put them back afterwards."""

    def __init__(self, *items):
        self.items = items  # (module, name, value)

    def __enter__(self):
        self.saved = [(m, n, getattr(m, n)) for m, n, _v in self.items]
        for m, n, v in self.items:
            setattr(m, n, v)
        return self

    def __exit__(self, *exc):
        for m, n, old in self.saved:
            setattr(m, n, old)
        return False


def article(**kw):
    """A row as the queue query returns it."""
    base = {"id": 1, "title": "Something about AI", "domain": "press.test", "published_at": NOW,
            "created_at": NOW, "discussion_points": None, "trend_score": None, "weight": 1.0,
            "source_type": "press", "category_hint": None}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------- the queue

def test_queue_sends_the_best_model_to_what_leads_a_story():
    front = {"anthropic", "valuation", "funding"}
    lab = article(id=1, title="Introducing Claude 5", domain="anthropic.com", source_type="primary")
    press = article(id=2, title="A quiet week in enterprise software", weight=1.2)
    weak = article(id=3, title="Top 10 AI tools for your business in 2026", weight=0.7)
    flood = article(id=4, title="Attention is all you need again", domain="arxiv.org", weight=1.0)
    focus = article(id=5, title="Google Ads adds an AI assistant", weight=1.0, category_hint="marketing")
    hot = article(id=6, title="A small lab ships a fast model", weight=1.0, discussion_points=450)
    follow = article(id=7, title="Anthropic valuation talks reach investors", weight=1.0)

    order = [r.id for r in enrich.order_queue([press, weak, flood, focus, hot, follow, lab], NOW, front)]
    assert order[0] == 1, order                      # the lab's own post first
    assert order.index(5) < order.index(2), order    # the focus category beats ordinary press
    assert order.index(6) < order.index(2), order    # what the web is reacting to beats ordinary press
    assert order.index(7) < order.index(2), order    # a follow-up to a front-page story beats it too
    assert order[-2:] == [3, 4] or order[-2:] == [4, 3], order  # a listicle and the preprint flood last
    # A preprint the web is reacting to is not treated as flood.
    assert enrich.queue_score(article(domain="arxiv.org", discussion_points=300), NOW) > enrich.queue_score(flood, NOW)


def test_queue_lets_nothing_starve():
    fresh_weak = article(id=1, title="Top 5 AI tools", domain="arxiv.org", created_at=NOW)
    old_weak = article(id=2, title="Top 5 AI tools", domain="arxiv.org", created_at=NOW - timedelta(days=2))
    fresh_press = article(id=3, weight=1.2, created_at=NOW)
    fresh_lab = article(id=4, title="Introducing Claude 5", domain="anthropic.com", source_type="primary", created_at=NOW)
    assert enrich.queue_score(old_weak, NOW) > enrich.queue_score(fresh_weak, NOW)
    assert enrich.queue_score(old_weak, NOW) > enrich.queue_score(fresh_press, NOW)
    # Waiting lifts an article, but a lab's own announcement from this hour still goes first.
    assert enrich.queue_score(fresh_lab, NOW) > enrich.queue_score(old_weak, NOW)
    # The age term is capped, so a week-old row cannot outrank everything for ever.
    week = article(id=5, title="Top 5 AI tools", domain="arxiv.org", created_at=NOW - timedelta(days=7))
    assert enrich.queue_score(week, NOW) == enrich.queue_score(old_weak, NOW)


def test_queue_reads_only_narrow_columns_and_keeps_the_oldest_in_view():
    with fresh_db() as eng:
        with eng.begin() as conn:
            conn.execute(insert(db.sources).values(id=1, key="press", name="Press", url="https://press.test/feed",
                                                   source_type="press", weight=1.0))
            for i in range(1, 40):
                conn.execute(insert(db.articles).values(
                    id=i, url=f"https://press.test/{i}", source_id=1, title=f"An AI story {i}", domain="press.test",
                    published_at=NOW - timedelta(hours=i), created_at=NOW - timedelta(hours=i), fetched_at=NOW,
                    status="gated", content_text="words " * 3000, description="d"))
        before = db.bytes_read()
        with eng.connect() as conn:
            picked = enrich.pick_queue(conn, 5)
        read_kb = (db.bytes_read() - before) / 1024
        assert len(picked) == 5
        assert read_kb < 40, read_kb  # the ranking pass never reads article text
        # The oldest waiting article is in the queue: an article cannot be passed over for days.
        assert 39 in picked, picked


# ---------------------------------------------------------------------------- the prompt

LONG_TEXT = "The company said the round values it at $3 billion. " * 600


def test_prompt_stays_inside_every_provider_window():
    row = SimpleNamespace(title="A title", source_name="Press", published_at=NOW)
    sizes = {}
    for provider in ("gemini", "cloud", "groq", "ollama"):
        prompt = enrich.build_prompt(row, LONG_TEXT, provider)
        sizes[provider] = enrich.estimated_tokens(prompt)
        cap = config.PROMPT_TOKEN_BUDGET.get(provider)
        if cap:
            assert sizes[provider] <= cap, (provider, sizes[provider], cap)
        # Every provider gets the whole schema: the blocks other parts of the pipeline read must
        # not fall out of the short version.
        for field in ('"work_card"', '"model_release"', '"funding"', '"key_points"', '"importance"',
                      '"is_ai_news"', '"entities"', '"content_type"', '"category"'):
            assert field in prompt, (provider, field)
        assert "Keep the source's hedging" in prompt
        assert "Copy every number" in prompt  # the figures rule reaches every provider
    assert sizes["groq"] < sizes["gemini"], sizes
    assert sizes["ollama"] < sizes["groq"], sizes
    # The worked examples are what the narrow windows trade away, not the rules.
    assert "Example 1" in enrich.build_prompt(row, "short", "gemini")
    assert "Example 1" not in enrich.build_prompt(row, "short", "groq")
    assert "Good, keeping a report's hedging" in enrich.build_prompt(row, "short", "groq")
    # The local model takes over when no key is configured: its prompt must fit its context too.
    assert enrich.estimated_tokens(enrich.build_prompt(row, LONG_TEXT, "gemini", local_only=True)) <= config.PROMPT_TOKEN_BUDGET["ollama"]


def test_prompt_and_categories_still_format_in_one_call():
    """Anything that formats the whole template (the older path) keeps working."""
    out = enrich.PROMPT.format(categories=enrich.categories_line(), title="T", source="S", published="P", text="X")
    assert '"marketing"' in out and "Article text:" in out and "{" not in out.split("Article text:")[1]


# ---------------------------------------------------------------------------- the check before storing

SOURCE = ("Mistral AI said on Tuesday it had raised 3 billion euros at a valuation of 14 billion euros. "
          "The round was led by ASML, with Nvidia joining. Chief executive Arthur Mensch said the money "
          "would go into data centres in France. The company reported 40% growth in enterprise customers "
          "over the past year and said its next model arrives in 2027.") * 2


def summary(**kw):
    base = {"headline": "Mistral raises 3 billion euros at a 14 billion euro valuation",
            "summary_md": "Mistral AI raised 3 billion euros, the company said on Tuesday. ASML led the round.",
            "key_points": ["ASML led the round", "Nvidia joined", "40% growth in enterprise customers"],
            "why_it_matters": "Europe gets a funded lab.",
            "entities": {"companies": ["Mistral AI", "ASML", "Nvidia"], "models": [], "people": ["Arthur Mensch"]}}
    base.update(kw)
    return base


def test_check_catches_a_wrong_number():
    bad = summary(headline="Mistral raises 5 billion euros at a 14 billion euro valuation")
    rep = checks.report(bad, SOURCE)
    assert "5 billion" in " ".join(rep["figures"]), rep
    assert "headline" in rep["where"], rep
    # A figure only in the body is caught as well.
    body = summary(summary_md="Mistral AI raised 3 billion euros and now has 900 employees, the company said.")
    assert checks.items(checks.report(body, SOURCE)) == ["900"], checks.report(body, SOURCE)


def test_check_catches_a_wrong_name():
    bad = summary(headline="Mistral and OpenAI raise 3 billion euros together",
                  entities={"companies": ["Mistral AI", "OpenAI"], "models": [], "people": []})
    rep = checks.report(bad, SOURCE)
    assert rep["names"] == ["OpenAI"], rep
    # A name the summary never uses is untidy, not wrong: it is not flagged.
    listed = summary(entities={"companies": ["Mistral AI", "Cohere"], "models": [], "people": []})
    assert checks.items(checks.report(listed, SOURCE)) == []


def test_check_passes_what_the_article_supports():
    assert checks.items(checks.report(summary(), SOURCE)) == []
    # Rounding and currency conversion of a large figure is not a mismatch.
    assert checks.items(checks.report(summary(headline="Mistral raises about $3.4 billion"), SOURCE)) == []
    # A year, and a figure written differently, do not produce noise.
    assert checks.items(checks.report(summary(summary_md="In 2027 the next model arrives; growth was 40%."), SOURCE)) == []
    # Too little source text to judge: nothing is flagged, so a feed stub never blocks a summary.
    assert checks.items(checks.report(summary(headline="Mistral raises 9 billion"), "Short feed line.")) == []


def test_safer_keeps_only_what_the_article_supports():
    bad = summary(headline="Mistral and OpenAI raise 5 billion euros",
                  summary_md=("Mistral AI raised 3 billion euros, the company said on Tuesday, at a valuation of "
                              "14 billion euros. "
                              "OpenAI took part with 5 billion euros of its own. "
                              "ASML led the round and Nvidia joined it, the company said in its announcement. "
                              "Arthur Mensch, the chief executive, said the money goes into data centres in France, "
                              "where the company is building out capacity with its partners. Mistral also reported "
                              "40% growth in enterprise customers over the past year, and said its next model "
                              "arrives in 2027."),
                  key_points=["ASML led the round", "OpenAI joined", "40% growth in enterprise customers"],
                  entities={"companies": ["Mistral AI", "OpenAI"], "models": [], "people": []})
    rep = checks.report(bad, SOURCE)
    row = SimpleNamespace(id=1, title="Mistral raises 3 billion euros, led by ASML")
    safe, changed = checks.safer(bad, rep, row, SOURCE)
    assert safe["headline"] == "Mistral raises 3 billion euros, led by ASML"
    assert "OpenAI" not in safe["summary_md"] and "5 billion" not in safe["summary_md"]
    assert "Mistral AI raised 3 billion euros" in safe["summary_md"]
    assert safe["key_points"] == ["ASML led the round", "40% growth in enterprise customers"]
    assert safe["entities"]["companies"] == ["Mistral AI"]
    assert changed and checks.items(checks.report(safe, SOURCE)) == []


def test_safer_falls_back_to_the_article_when_nothing_is_left():
    bad = summary(summary_md="Mistral raised 5 billion euros.", key_points=["5 billion euros"])
    rep = checks.report(bad, SOURCE)
    safe, changed = checks.safer(bad, rep, SimpleNamespace(id=1, title="Mistral raises 3 billion euros"), SOURCE)
    assert "Mistral AI said on Tuesday" in safe["summary_md"]
    assert "replaced with the article's opening" in " ".join(changed)


def test_verify_retries_once_and_then_repairs():
    with fresh_db() as eng:
        row = SimpleNamespace(id=1, title="Mistral raises 3 billion euros, led by ASML")
        calls = []

        def good_second(prompt):
            calls.append(prompt)
            return summary()

        stats = {"checked": 0, "flagged": 0, "retried": 0, "edited": 0}
        bad = summary(headline="Mistral raises 5 billion euros")
        clean, note = enrich.verify(eng, row, SOURCE, bad, (good_second, "PROMPT", "groq"), {"groq": 5}, {}, stats)
        assert stats == {"checked": 1, "flagged": 1, "retried": 1, "edited": 0}
        assert "5 billion" in calls[0] and "only figures" in calls[0]
        assert clean["headline"] == summary()["headline"] and note["fixed"] == "retry"

        # A second answer that is wrong again is repaired instead, and nothing is asked a third time.
        stats = {"checked": 0, "flagged": 0, "retried": 0, "edited": 0}
        clean, note = enrich.verify(eng, row, SOURCE, bad, (lambda p: bad, "PROMPT", "groq"), {"groq": 5}, {}, stats)
        assert stats["retried"] == 1 and stats["edited"] == 1
        assert clean["headline"] == row.title and note["fixed"] == "edited"

        # No provider left to ask (the heuristic path): repaired straight away, no request made.
        stats = {"checked": 0, "flagged": 0, "retried": 0, "edited": 0}
        clean, _note = enrich.verify(eng, row, SOURCE, bad, None, {}, {}, stats)
        assert stats["retried"] == 0 and clean["headline"] == row.title
        # The retry is bounded per run.
        stats = {"checked": 0, "flagged": 0, "retried": config.CHECK_RETRY_MAX_PER_RUN, "edited": 0}
        enrich.verify(eng, row, SOURCE, bad, (lambda p: bad, "P", "groq"), {"groq": 5}, {}, stats)
        assert stats["retried"] == config.CHECK_RETRY_MAX_PER_RUN


def test_the_dashboard_says_what_was_caught():
    stats = {"checks": {"checked": 12, "flagged": 2, "retried": 1, "edited": 1,
                        "caught": [{"id": 3, "headline": "Mistral raises", "items": ["5 billion"], "where": "headline",
                                    "fixed": "edited", "changed": ["headline replaced with the source's own title"]}]}}
    rows = [{"step": "enrich", "startedAt": NOW - timedelta(minutes=20), "stats": stats}]
    cards = admin.check_cards(rows, NOW)
    assert len(cards) == 1 and cards[0]["level"] == "info"
    assert "2 summaries" in cards[0]["what"] and "12 were checked" in cards[0]["why"]
    assert cards[0]["items"][0]["detail"].startswith('The headline said "5 billion"')
    # Nothing caught, no card.
    assert admin.check_cards([{"step": "enrich", "startedAt": NOW, "stats": {"enriched": 3}}], NOW) == []
    # A quarter of all summaries flagged is a model problem, and says so louder.
    many = {"checks": {"checked": 20, "flagged": 9, "retried": 4, "edited": 5, "caught": []}}
    assert admin.check_cards([{"step": "enrich", "startedAt": NOW, "stats": many}], NOW)[0]["level"] == "warning"


# ---------------------------------------------------------------------------- the second pass

def test_upgrade_rule_is_bounded_and_never_repeats_itself():
    weak = "groq:qwen3.8-27b"
    strong = "gemini:gemini-3.8-flash"
    # A weak summary on a story that proved important: yes.
    assert upgrade.should_upgrade(sources=1, score=0.7, engagement=0.0, enrich_model=weak)
    assert upgrade.should_upgrade(sources=1, score=0.2, engagement=4.0, enrich_model=weak)
    # A weak summary nobody reads on a story nobody else covered: no.
    assert not upgrade.should_upgrade(sources=1, score=0.2, engagement=0.0, enrich_model=weak)
    # A strong summary of a single-source story: nothing to gain.
    assert not upgrade.should_upgrade(sources=1, score=0.9, engagement=9.0, enrich_model=strong)
    # Three independent sources are worth a digest written from all of them, however good the first was.
    assert upgrade.should_upgrade(sources=3, score=0.1, engagement=0.0, enrich_model=strong)
    # Done once, never again for the same material.
    done = upgrade.mark(strong, 3)
    assert upgrade.upgraded_with(done) == 3 and upgrade.provider_of(done) == "gemini"
    assert not upgrade.should_upgrade(sources=3, score=0.9, engagement=9.0, enrich_model=done)
    assert not upgrade.should_upgrade(sources=5, score=0.9, engagement=9.0, enrich_model=done)
    assert upgrade.should_upgrade(sources=6, score=0.1, engagement=0.0, enrich_model=done)
    assert len(upgrade.mark("cloud:" + "m" * 80, 4)) <= 60  # the column holds 60 characters


def test_spare_allowance_is_what_the_day_is_ahead_by():
    with fresh_db() as eng:
        with eng.connect() as conn:
            for hour, expected_positive in ((23, True), (0, False)):
                with Patch((db, "utcnow", lambda: NOW.replace(hour=hour, minute=0))):
                    assert (enrich.spare(conn, "gemini") > 0) is expected_positive
        # A day that has spent its share is never ahead.
        enrich.record_usage(eng, "gemini", config.DAILY_BUDGET["gemini"])
        with eng.connect() as conn, Patch((db, "utcnow", lambda: NOW.replace(hour=23, minute=0))):
            assert enrich.spare(conn, "gemini") <= 0


def test_pick_articles_takes_one_per_publisher_lead_first():
    def member(i, domain, importance=5, points=None, words=500):
        return SimpleNamespace(id=i, domain=domain, importance=importance, discussion_points=points, word_count=words)

    members = [member(1, "press.test"), member(2, "wire.test", importance=8), member(3, "press.test", importance=9),
               member(4, "lab.test", importance=6, points=200), member(5, "blog.test", importance=3)]
    picked = upgrade.pick_articles(1, members, 4)
    assert [m.id for m in picked] == [1, 2, 4, 5], [m.id for m in picked]
    assert upgrade.independent_sources(members) == 4
    # Without a usable lead the strongest article leads.
    assert upgrade.pick_articles(None, members, 2)[0].id == 3


def test_multi_prompt_carries_every_source_and_asks_for_the_differences():
    rows = [SimpleNamespace(id=1, domain="reuters.test", source_name="Reuters", published_at=NOW,
                            title="Anthropic raises $30bn", text="Reuters says the round is $30 billion. " * 40),
            SimpleNamespace(id=2, domain="theinformation.test", source_name="The Information", published_at=NOW,
                            title="Anthropic close to a deal", text="The Information says $28 billion. " * 40)]
    prompt = upgrade.build_multi_prompt(rows)
    assert "Article 1 of 2" in prompt and "Article 2 of 2" in prompt
    assert "reuters.test" in prompt and "theinformation.test" in prompt
    assert "where they differ" in prompt and "agree" in prompt
    assert enrich.estimated_tokens(prompt) < 6000, enrich.estimated_tokens(prompt)


MULTI_ANSWER = {
    "headline": "Anthropic is reportedly raising at a $350 billion valuation",
    "summary_md": ("Anthropic is raising a round at a valuation of about $350 billion, Reuters and The Information "
                   "both reported on Tuesday. Both say the round is led by existing investors and that no terms "
                   "have been signed yet.\n\nThe two differ on size: Reuters puts the round at $30 billion, while "
                   "The Information reports $28 billion. Anthropic has not commented on either figure, and neither "
                   "outlet names the investors on the record."),
    "key_points": ["Reuters puts the round at $30 billion", "The Information reports $28 billion",
                   "Neither outlet names an investor on the record"],
    "why_it_matters": "The size of the round sets what Anthropic can spend on compute next year.",
    "entities": {"companies": ["Anthropic"], "models": [], "people": []},
    "importance": 8,
}


def seed_story(eng, *, sources=3, model="groq:qwen3.8-27b", score=0.7):
    first = NOW - timedelta(hours=6)
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="press", name="Press", url="https://press.test/feed",
                                               source_type="press", weight=1.0))
        conn.execute(insert(db.stories).values(
            id=1, slug="anthropic-round", headline="Anthropic raises at a $350 billion valuation",
            summary_md="One outlet reported a round.", key_points=["A round"], why_it_matters="Money.",
            category="business", entities={"companies": ["Anthropic"], "models": [], "people": []},
            lead_article_id=1, article_count=sources, importance=6, score=score, status="published",
            first_published_at=first, updated_at=first + timedelta(hours=1)))
        for i in range(1, sources + 1):
            conn.execute(insert(db.articles).values(
                id=i, url=f"https://pub{i}.test/{i}", source_id=1, story_id=1, slug=f"a-{i}",
                title=f"Anthropic round, report {i}", raw_title=f"Anthropic round, report {i}",
                headline=f"Anthropic round, report {i}", domain=f"pub{i}.test", published_at=first,
                fetched_at=first, created_at=first, description="Anthropic is raising money.",
                content_md=("Anthropic is raising a round at about a $350 billion valuation, according to people "
                            "familiar with the talks. Reuters puts the round at $30 billion. " * 12),
                content_text="Anthropic is raising a round. " * 40, word_count=400, status="published",
                summary_md="A round.", key_points=["A round"], why_it_matters="Money.", category="business",
                entities={"companies": ["Anthropic"], "models": [], "people": []}, content_type="news",
                importance=6, enrich_model=model if i == 1 else "groq:qwen3.8-27b", engagement=0.0))


def story_row(eng):
    with eng.connect() as conn:
        return conn.execute(select(db.stories)).mappings().first()


def test_upgrade_writes_a_multi_source_digest_and_leaves_freshness_alone():
    with fresh_db() as eng:
        seed_story(eng)
        before = story_row(eng)
        calls = []

        def fake_gemini(prompt):
            calls.append(prompt)
            return dict(MULTI_ANSWER)

        with Patch((enrich, "call_gemini", fake_gemini), (config, "GEMINI_API_KEY", "test-key"),
                   (config, "OLLAMA_API_KEY", ""), (enrich, "spare", lambda conn, p: 5),
                   (upgrade.time, "sleep", lambda s: None)):
            stats = upgrade.run()
            assert stats["upgraded"] == 1 and stats["multi"] == 1, stats
            assert "Article 3 of 3" in calls[0], "the digest reads every independent source"

            after = story_row(eng)
            assert "Reuters puts the round at $30 billion" in after["summary_md"]
            assert after["key_points"] == MULTI_ANSWER["key_points"] and after["importance"] == 8
            # Freshness is untouched: an improved summary must not make an old story look new.
            assert after["first_published_at"] == before["first_published_at"]
            assert after["updated_at"] == before["updated_at"]
            with eng.connect() as conn:
                lead = conn.execute(select(db.articles).where(db.articles.c.id == 1)).mappings().first()
            assert lead["enrich_model"] == "gemini:gemini-3.8-flash+u3"
            assert lead["work_card"] is None and lead["funding"] is None  # a multi digest touches neither

            # The same story is not written again for the same sources, and the day's cap holds.
            cache.reset()
            again = upgrade.run()
            assert again["upgraded"] == 0 and again["candidates"] == 0, again
            with eng.connect() as conn:
                assert enrich.usage_today(conn, "upgrade")[0] == 1
                assert enrich.usage_today(conn, "gemini")[0] == 1


def test_upgrade_stands_aside_while_articles_wait_and_when_the_day_is_behind():
    with fresh_db() as eng:
        seed_story(eng)
        with eng.begin() as conn:
            for i in range(50):
                conn.execute(insert(db.articles).values(
                    url=f"https://q.test/{i}", source_id=1, title="Waiting", domain="q.test", fetched_at=NOW,
                    created_at=NOW, status="gated"))
        with Patch((enrich, "call_gemini", lambda p: dict(MULTI_ANSWER)), (config, "GEMINI_API_KEY", "k"),
                   (enrich, "spare", lambda conn, p: 5), (upgrade.time, "sleep", lambda s: None)):
            assert upgrade.run()["skipped"] == "articles still waiting for a first summary"
    with fresh_db() as eng:
        seed_story(eng)
        with Patch((config, "GEMINI_API_KEY", "k"), (enrich, "spare", lambda conn, p: 0),
                   (upgrade.time, "sleep", lambda s: None)):
            assert upgrade.run()["skipped"] == "no spare allowance"
    with fresh_db() as eng:
        seed_story(eng)
        enrich.record_usage(db.engine(), "upgrade", config.UPGRADE_DAILY_MAX)
        with Patch((config, "GEMINI_API_KEY", "k"), (enrich, "spare", lambda conn, p: 5),
                   (upgrade.time, "sleep", lambda s: None)):
            assert upgrade.run()["skipped"] == "daily upgrade limit reached"


def test_upgrade_checks_its_own_answer_against_the_sources():
    with fresh_db() as eng:
        seed_story(eng)
        wrong = dict(MULTI_ANSWER, headline="Anthropic raises at a $900 billion valuation",
                     summary_md=MULTI_ANSWER["summary_md"] + " SoftBank led the round with $900 billion.",
                     entities={"companies": ["Anthropic", "SoftBank"], "models": [], "people": []})
        answers = [wrong, wrong]
        with Patch((enrich, "call_gemini", lambda p: answers.pop(0)), (config, "GEMINI_API_KEY", "k"),
                   (config, "OLLAMA_API_KEY", ""), (enrich, "spare", lambda conn, p: 5),
                   (upgrade.time, "sleep", lambda s: None)):
            stats = upgrade.run()
        assert stats["upgraded"] == 1 and stats["checks"]["flagged"] == 1, stats
        after = story_row(eng)
        assert "900 billion" not in after["summary_md"] and "SoftBank" not in after["summary_md"]
        assert "900 billion" not in after["headline"]
        assert stats["caught"][0]["items"]


def test_upgrade_falls_back_to_the_single_article_prompt_for_a_lone_source():
    with fresh_db() as eng:
        seed_story(eng, sources=1, score=0.8)
        answer = {**MULTI_ANSWER, "category": "business", "content_type": "news", "is_ai_news": True,
                  "summary_md": ("Anthropic is raising a round at about a $350 billion valuation, people familiar "
                                 "with the talks say. Reuters puts the round at $30 billion, and says no terms "
                                 "have been signed yet.\n\nNeither the company nor any investor has commented on "
                                 "the record, and the report does not say when the round would close. The figure "
                                 "would be the largest raised by a private AI company so far, on the reported "
                                 "numbers, and none of it is confirmed by Anthropic itself at this point."),
                  "key_points": ["Reuters puts the round at $30 billion"], "work_card": None,
                  "funding": {"company": "Anthropic", "amount_usd": 30e9, "round": "series_f"}}
        prompts = []
        with Patch((enrich, "call_gemini", lambda p: prompts.append(p) or dict(answer)), (config, "GEMINI_API_KEY", "k"),
                   (config, "OLLAMA_API_KEY", ""), (enrich, "spare", lambda conn, p: 5),
                   (upgrade.time, "sleep", lambda s: None)):
            stats = upgrade.run()
        assert stats["single"] == 1 and stats["multi"] == 0, stats
        assert "Article text:" in prompts[0] and "Article 1 of" not in prompts[0]
        with eng.connect() as conn:
            lead = conn.execute(select(db.articles).where(db.articles.c.id == 1)).mappings().first()
        assert lead["enrich_model"].endswith("+u1")
        assert (lead["funding"] or {}).get("company") == "Anthropic"  # the better model's extras are kept


def test_upgrade_reads_kilobytes_not_megabytes():
    with fresh_db() as eng:
        seed_story(eng)
        with Patch((enrich, "call_gemini", lambda p: dict(MULTI_ANSWER)), (config, "GEMINI_API_KEY", "k"),
                   (config, "OLLAMA_API_KEY", ""), (enrich, "spare", lambda conn, p: 5),
                   (upgrade.time, "sleep", lambda s: None)):
            before = db.bytes_read()
            upgrade.run()
            first_kb = (db.bytes_read() - before) / 1024
            # Nothing to do: the mirror answers from the runner's copy.
            cache.save_all()
            cache.reset()
            before = db.bytes_read()
            upgrade.run()
            idle_kb = (db.bytes_read() - before) / 1024
        assert first_kb < 120, first_kb
        assert idle_kb < 40, idle_kb


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
