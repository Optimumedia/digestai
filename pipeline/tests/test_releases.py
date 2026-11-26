"""The model tracker keeps launches after their stories leave the export window:
python tests/test_releases.py (offline, temporary cache directory)."""
from __future__ import annotations

import gzip
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import archive, config, releases  # noqa: E402
from digest.trackers import fold_versions, is_release, model_key  # noqa: E402


@contextmanager
def cache_dir():
    tmp = Path(tempfile.mkdtemp())
    saved = config.CACHE_DIR
    config.CACHE_DIR = tmp / "cache"
    try:
        yield tmp
    finally:
        config.CACHE_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


def row(name, lab, date, slug, headline, **extra):
    return {"name": name, "lab": lab, "availability": extra.pop("availability", "api"), "kind": "llm", "license": None,
            "context": None, "link": None, "date": date, "storySlug": slug, "storyHeadline": headline, "sources": 1, **extra}


def keyed(*rows):
    return {model_key(r["name"], r["lab"]): r for r in rows}


def run(current, unpublished=()):
    return releases.remember(current, list(unpublished), is_release, fold_versions)


def test_a_launch_outlives_the_window_and_keeps_its_first_date_and_story():
    with cache_dir():
        launch = row("GPT-6", "OpenAI", "2026-09-10T09:00:00Z", "openai-releases-gpt-6", "OpenAI releases GPT-6")
        first = run(keyed(launch))
        assert [r["name"] for r in first.values()] == ["GPT-6"]
        # Weeks later the launch story has left the window; another story about the model adds a spec.
        later = row("GPT-6", "OpenAI", "2026-10-20T09:00:00Z", "gpt-6-gets-bigger-window", "OpenAI launches GPT-6 with a 2M window",
                    context="2M tokens", sources=3)
        kept = run(keyed(later, row("Opus 5.5", "Anthropic", "2026-10-21T09:00:00Z", "opus", "Anthropic releases Opus 5.5")))
        gpt = kept[model_key("GPT-6", "OpenAI")]
        assert gpt["date"] == "2026-09-10T09:00:00Z" and gpt["storySlug"] == "openai-releases-gpt-6", gpt
        assert gpt["context"] == "2M tokens" and gpt["sources"] == 3, gpt
        # A run whose window holds neither still lists both.
        assert sorted(r["name"] for r in run({}).values()) == ["GPT-6", "Opus 5.5"]


def test_an_earlier_report_replaces_the_kept_launch():
    with cache_dir():
        run(keyed(row("Muse", "Meta", "2026-10-05T09:00:00Z", "b", "Meta launches Muse")))
        out = run(keyed(row("Muse", "Meta", "2026-10-01T09:00:00Z", "a", "Meta launches Muse", license="Llama")))
        muse = next(iter(out.values()))
        assert muse["storySlug"] == "a" and muse["license"] == "Llama", muse


def test_unpublished_stories_and_rows_the_rules_now_reject_are_dropped():
    with cache_dir():
        run(keyed(row("GPT-6", "OpenAI", "2026-09-10T09:00:00Z", "pulled", "OpenAI releases GPT-6"),
                  row("Mystery", "Unknown", "2026-09-11T09:00:00Z", "m", "Mystery model launches")))
        out = run({}, unpublished=["pulled"])
        assert out == {}, out


def test_versionless_kept_row_folds_into_the_versioned_launch():
    with cache_dir():
        run(keyed(row("WeatherNext", "Google", "2026-09-01T09:00:00Z", "w", "Google releases WeatherNext")))
        out = run(keyed(row("WeatherNext 3", "Google", "2026-09-03T09:00:00Z", "w3", "Google releases WeatherNext 3")))
        assert [r["name"] for r in out.values()] == ["WeatherNext 3"], out
        assert next(iter(out.values()))["date"] == "2026-09-01T09:00:00Z"


def test_a_lost_cache_is_restored_from_the_archive_copy_and_a_damaged_file_is_ignored():
    with cache_dir():
        run(keyed(row("GPT-6", "OpenAI", "2026-09-10T09:00:00Z", "g", "OpenAI releases GPT-6")))
        rows = releases.read_file()
        # The archive keeps a copy (archive.update writes it next to its stories).
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(archive.path(), "wt", encoding="utf-8") as f:
            json.dump({"format": archive.FORMAT, "windowStart": "2026-10-01T00:00:00+00:00", "stories": {}, releases.ARCHIVE_KEY: rows}, f)
        releases.path().write_text("{not json", encoding="utf-8")
        assert releases.read_file() is None
        assert [r["name"] for r in releases.load().values()] == ["GPT-6"]
        releases.path().unlink()
        assert [r["name"] for r in run({}).values()] == ["GPT-6"]


def test_kept_rows_are_keyed_again_by_todays_rules():
    with cache_dir():
        # Kept under an older key rule, which read "OpenAI Group PBC" as a lab of its own.
        old = row("GPT-6 Cyber", "OpenAI Group PBC", "2026-11-05T09:00:00Z", "cyber", "OpenAI releases GPT-6 Cyber")
        releases.save({"gpt6cyber|openaigrouppbc": old})
        new = row("GPT-6 Cyber", "OpenAI", "2026-11-06T09:00:00Z", "cyber-2", "OpenAI launches GPT-6 Cyber for defenders")
        out = releases.remember(keyed(new), [], is_release, fold_versions, model_key)
        assert list(out) == [model_key("GPT-6 Cyber", "OpenAI")], out
        assert next(iter(out.values()))["storySlug"] == "cyber"


def test_nothing_kept_and_nothing_new_is_an_empty_tracker():
    with cache_dir():
        assert run({}) == {}
        assert json.loads(releases.path().read_text(encoding="utf-8")) == {"format": releases.FORMAT, "rows": {}}


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
