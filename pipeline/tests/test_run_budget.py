"""The run's time budget (digest/run.py): steps that can wait give way to the ones that cannot."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import run  # noqa: E402

B = 1080


def test_a_slow_day_skips_what_can_wait_and_keeps_the_last_steps():
    steps = run.ORDER
    i = steps.index
    # With summaries held to eight minutes, the export is done about ten minutes in: topics still runs.
    assert not run.should_wait("topics", steps, i("topics"), 615, B)
    # 15 minutes gone: topics and images wait; admin, the team and media never do.
    assert run.should_wait("topics", steps, i("topics"), 900, B)
    assert run.should_wait("images", steps, i("images"), 900, B)
    for step in ("export", "audio", "media", "backup", "admin", "team", "morning", "notify", "readme", "indexnow"):
        assert not run.should_wait(step, steps, i(step), 1070, B), step
    # The time kept covers every always-run step still to come, and nothing already done.
    assert run.time_kept(steps, i("simplify")) == sum(run.LAST.values())
    assert run.time_kept(steps, i("admin")) == sum(run.LAST[s] for s in ("team", "morning", "notify", "readme", "indexnow"))
    # Every step named in the two lists exists, and none is in both.
    assert set(run.CAN_WAIT) <= set(steps) and set(run.LAST) <= set(steps) and not set(run.CAN_WAIT) & set(run.LAST)


def test_topics_write_only_the_counts_that_changed():
    """All 3,400 topics were rewritten every run, one round trip each: six minutes on 23 Nov 2026."""
    import json

    import test_reads as tr
    from sqlalchemy import select

    from digest import config, db, topics

    with tr.fresh_db() as (eng, _tmp):
        config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.SITE_DATA_DIR / "stories.json").write_text(json.dumps([{"id": i} for i in range(1, 6)]), encoding="utf-8")

        def entities(openai_ids):
            ents = [{"name": "OpenAI", "kind": "companies", "storyIds": openai_ids},
                    {"name": "Mistral", "kind": "companies", "storyIds": [4]}]
            (config.SITE_DATA_DIR / "entities.json").write_text(json.dumps(ents), encoding="utf-8")

        saved = (config.GROQ_API_KEY, config.GEMINI_API_KEY)
        config.GROQ_API_KEY = config.GEMINI_API_KEY = ""  # no model: only the sync runs
        try:
            entities([1, 2])
            assert topics.run()["topics"] == 2
            entities([1, 2])
            assert "updated" not in topics.run()  # nothing changed: nothing written
            entities([1, 2, 3])
            st = topics.run()
            assert st["updated"] == 1, st
        finally:
            config.GROQ_API_KEY, config.GEMINI_API_KEY = saved
        with eng.connect() as conn:
            counts = dict(conn.execute(select(db.topics.c.slug, db.topics.c.story_count)).all())
        assert counts == {"openai": 3, "mistral": 1}, counts


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
