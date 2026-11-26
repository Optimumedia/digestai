"""Story grouping repairs on a temporary database (SQLite, or Postgres with TEST_POSTGRES_URL as the
postgres-tests workflow does): python tests/test_grouping.py"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from digest import db, repair  # noqa: E402
from test_reads import NOW, fresh_db, new_process, seed  # noqa: E402


def test_repair_hides_roundups_and_replaces_late_leads():
    """The 25 Nov sample: a note.com round-up led "Anthropic raises $81.3 billion" over a story about
    Gemini 4 Neon, six round-ups made a story of their own, and an overview five days on led the
    Gemini 4 Neon launch story."""
    with fresh_db() as (eng, _tmp):
        seed(eng, stories=4, per_story=3)  # story s holds articles 3s-2..3s, led by the first
        with eng.begin() as conn:
            a = db.articles.c
            conn.execute(update(db.articles).where(a.id == 1).values(title="AI News Flash: Anthropic Raises $81.3 Billion (November 23, 2026 Issue)"))
            for aid in (4, 5, 6):
                conn.execute(update(db.articles).where(a.id == aid).values(title=f"AI News Weekly Summary #{aid}0 | GPT-6.2, Gemini 4 Neon"))
            first3 = conn.execute(select(db.stories.c.first_published_at).where(db.stories.c.id == 3)).scalar()
            conn.execute(update(db.articles).where(a.id == 7).values(published_at=first3 + timedelta(days=3)))
        new_process()
        done = repair.story_leads(eng)
        assert done == {"roundups_hidden": 1, "stories_unpublished": 1, "leads_changed": 2}, done
        with eng.connect() as conn:
            stories = {s.id: s for s in conn.execute(select(db.stories)).all()}
            arts = {r.id: r for r in conn.execute(select(db.articles)).all()}
        assert arts[1].status == "overflow" and arts[1].story_id == 1, "the round-up still counts, unseen"
        assert stories[1].lead_article_id == 3 and stories[1].headline == "Headline 3", "a primary report leads instead"
        assert stories[2].status == "unpublished", "nothing but round-ups"
        assert stories[3].lead_article_id == 9 and stories[3].headline == "Headline 9", "the late overview gave way"
        assert stories[3].summary_md == arts[9].summary_md and stories[3].source_notes is None
        assert stories[4].lead_article_id == 10 and stories[4].headline == "Story 4", "a sound story is left alone"
        new_process()
        assert repair.story_leads(eng) == {}, "nothing left to do on the next run"


def test_a_thread_takes_only_stories_that_say_what_its_headlines_say():
    """Episodes from the 26 Nov thread pages, with the similarity of their stories."""
    from digest import threads

    ubiquitous = {"nvidia", "anthropic", "openai"}
    common = {"nvidia", "anthropic", "openai"}
    chips = {"nvidia", "megaspeed", "alice huang", "jensen huang"}
    words = set()
    for h in ("About 600,000 Nvidia H200 chips are stored in Hong Kong awaiting shipment to mainland China",
              "US approved Nvidia H200 chip sales to two Chinese firms with empty offices and sanctioned ties",
              "Trump approves curbs on companies diverting Nvidia chips to China"):
        words |= threads.event_words(h, common)
    rubio = threads.event_words("Anthropic paid $740k to firm run by Rubio's ex-aide", common)
    assert not threads.joins_thread(0.79, {"anthropic", "jensen huang"}, chips, ubiquitous, rubio, words)
    rent = threads.event_words("Chinese AI firms rent overseas Nvidia capacity fearing tighter U.S. remote-access rules", common)
    assert threads.joins_thread(0.79, {"nvidia", "megaspeed"}, chips, ubiquitous, rent, words), "chinese: the saga's word"
    smuggling = threads.event_words("Nvidia still faces inquiries over alleged GPU smuggling to China", common)
    assert threads.joins_thread(0.89, {"nvidia"}, chips, ubiquitous, smuggling, words), "very close: any shared name"
    assert not threads.joins_thread(0.95, {"openai"}, chips, ubiquitous, rent, words), "no shared name at all"


def test_threads_step_runs_with_the_word_rule():
    from digest import threads

    with fresh_db() as (eng, _tmp):
        seed(eng, stories=8, per_story=2)
        new_process()
        stats = threads.run()
        assert stats["assigned"] + stats["new_threads"] == 5, stats  # stories 4-8 had no thread


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - report every test, not just the first error
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
