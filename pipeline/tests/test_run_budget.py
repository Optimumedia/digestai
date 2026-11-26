"""The run's time budget (digest/run.py): steps that can wait give way to the ones that cannot."""
from __future__ import annotations

import os
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


def test_summaries_wait_for_the_local_model_only_while_it_is_starting():
    import tempfile

    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

        def sleep(self, s):
            self.t += s

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "ollama.state"
        clock = Clock()
        # No state file: the workflow did not start it in the background, so there is nothing to wait for.
        assert run.wait_for_local_model(str(state), 180, clock.sleep, clock) == 0.0
        for done in ("ready", "failed"):
            state.write_text(done + "\n", encoding="utf-8")
            assert run.wait_for_local_model(str(state), 180, clock.sleep, clock) == 0.0
        # Starting: wait until it is ready...
        state.write_text("starting", encoding="utf-8")
        clock = Clock()

        def sleep(s):
            clock.sleep(s)
            if clock.t >= 10:
                state.write_text("ready", encoding="utf-8")

        assert run.wait_for_local_model(str(state), 180, sleep, clock) == 10.0
        # ...and never longer than the limit when it hangs.
        state.write_text("starting", encoding="utf-8")
        clock = Clock()
        assert run.wait_for_local_model(str(state), 30, clock.sleep, clock) == 30.0
    # Without OLLAMA_STATE_FILE (a local run) nothing waits.
    saved = os.environ.pop("OLLAMA_STATE_FILE", None)
    try:
        assert run.wait_for_local_model() == 0.0
    finally:
        if saved is not None:
            os.environ["OLLAMA_STATE_FILE"] = saved


def test_a_crashed_step_is_an_error_on_the_run_page_and_the_summary_lists_every_step():
    summary = {"fetch": {"seconds": 66.1}, "export": {"crashed": True, "seconds": 3.2},
               "topics": {"skipped": "time", "left": 381, "seconds": 0.0}, "push": {"skipped": "", "seconds": 0.7}}
    text, errors = run.step_report(summary)
    assert len(errors) == 1 and errors[0].startswith("::error ") and "export" in errors[0]
    assert "| export | 3.2 | crashed |" in text and "| topics | 0.0 | skipped: time |" in text and "| push | 0.7 |  |" in text
    assert text.startswith("### Pipeline steps: 70 s, 1 left for the next run for time (topics)")
    assert run.step_report({"fetch": {"seconds": 1}})[1] == []


def test_a_chained_run_starts_twenty_minutes_after_the_last_one_started():
    from datetime import datetime, timezone

    from digest import pace

    at = datetime(2026, 11, 26, 9, 27, 50, tzinfo=timezone.utc).timestamp()  # when run 45786443849 queued the next
    started = datetime(2026, 11, 26, 9, 7, 7, tzinfo=timezone.utc).timestamp()  # its pipeline job's start
    # The old rule: 19 minutes 10 seconds to the 47 slot, so runs started 40 minutes apart.
    assert pace.until_slot(at) == 19 * 60 + 10
    # Now: the last run started more than 20 minutes ago, so this one starts at once.
    assert pace.seconds_to_wait(at, str(int(started)))[0] == 0
    # A short run: the next waits until 20 minutes after the last start, never less apart.
    assert pace.seconds_to_wait(started + 15 * 60, int(started))[0] == 5 * 60
    assert pace.seconds_to_wait(started + 60, int(started), minutes=20)[0] == 19 * 60
    # No usable start time (an older workflow, a manual run, a time from the future or from hours ago):
    # the next 7, 27 or 47 past the hour, as before.
    for after in ("", None, "abc", str(int(at + 3600)), str(int(at - 7 * 3600))):
        wait, why = pace.seconds_to_wait(at, after)
        assert wait == 19 * 60 + 10 and "next" in why, (after, wait)
    late = datetime(2026, 11, 26, 9, 50, 0, tzinfo=timezone.utc).timestamp()
    assert pace.until_slot(late) == 17 * 60  # wraps to 7 past the next hour


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
