"""The README's daily "Today in AI" section (pipeline/digest/readme.py). Offline, with a fake GitHub:
python tests/test_readme.py"""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import config, readme  # noqa: E402

NOW = datetime(2026, 10, 14, 9, 7, tzinfo=timezone.utc)
STORIES = [
    {"id": i, "slug": f"story-{i}", "headline": f"Headline {i}", "articleCount": n}
    for i, n in ((1, 27), (2, 4), (3, 1), (4, 2), (5, 2), (6, 9))
]


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, json.dumps(body or {})

    def json(self):
        return self._body


class FakeGitHub:
    def __init__(self, text):
        self.text, self.sha, self.puts = text, "abc", []

    def get(self, url, **kw):
        return Resp(200, {"sha": self.sha, "content": base64.b64encode(self.text.encode()).decode()})

    def put(self, url, json=None, **kw):
        self.puts.append(json)
        self.text = base64.b64decode(json["content"]).decode()
        return Resp(200, {})


def with_data(fn):
    tmp = Path(tempfile.mkdtemp())
    saved = config.SITE_DATA_DIR
    config.SITE_DATA_DIR = tmp
    (tmp / "briefing.json").write_text(json.dumps({"storyIds": [1, 2, 3, 4, 5, 6]}), encoding="utf-8")
    (tmp / "stories.json").write_text(json.dumps(STORIES), encoding="utf-8")
    try:
        fn()
    finally:
        config.SITE_DATA_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_section_lists_five_stories_with_sources_and_links():
    text = readme.section(STORIES, NOW, "https://digestai.news")
    assert "## Today in AI, 14 October 2026" in text
    assert "1. [Headline 1](https://digestai.news/story/story-1) (27 sources)" in text
    assert "3. [Headline 3](https://digestai.news/story/story-3) (1 source)" in text
    assert "Headline 6" not in text
    assert text.startswith(readme.MARK_START) and text.endswith(readme.MARK_END)


def test_splice_inserts_once_then_replaces_only_the_section():
    doc = "# Digest AI\n\nIntro.\n\n## Pipeline steps\n\nTable."
    first = readme.splice(doc, readme.section(STORIES, NOW, "https://x"))
    assert first.index(readme.MARK_START) > first.index("# Digest AI") and first.index(readme.MARK_END) < first.index("## Pipeline steps")
    later = readme.splice(first, readme.section(STORIES[1:], NOW, "https://x"))
    assert later.count(readme.MARK_START) == 1 and "Headline 1]" not in later
    assert later.endswith("## Pipeline steps\n\nTable.")


def test_one_commit_a_day_after_the_briefing_settles():
    def body():
        gh = FakeGitHub("# Digest AI\n\nIntro.")
        assert readme.run(session=gh, now=NOW.replace(hour=5)) == {"skipped": "before the day's briefing settles"}
        assert readme.run(session=gh, now=NOW)["updated"]
        assert len(gh.puts) == 1 and gh.puts[0]["sha"] == "abc" and gh.puts[0]["message"] == "README: today in AI, 14 Oct 2026"
        assert readme.run(session=gh, now=NOW.replace(hour=15)) == {"skipped": "already today"}
        assert len(gh.puts) == 1
        assert readme.run(session=gh, now=NOW.replace(day=15))["updated"] and len(gh.puts) == 2
        assert "## Today in AI, 15 October 2026" in gh.text and "14 October" not in gh.text
    with_data(body)


def test_no_token_does_nothing():
    import os
    saved = {k: os.environ.pop(k, None) for k in ("GH_TOKEN", "GITHUB_TOKEN")}
    try:
        assert readme.run(now=NOW) == {"skipped": "no token"}
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("all passed")
