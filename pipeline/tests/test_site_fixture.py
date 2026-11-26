"""The site's test data: python tests/test_site_fixture.py

tests/site_fixture.py runs made-up stories through the real export, admin and team steps; CI builds
the site from the result. These checks keep it able to do that job: the export still runs on it, it
holds what the built-site tests look for, every link in it points at a made-up .test address (so no
publisher's text or page is in it), and it never replaces a real export by accident."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import site_fixture  # noqa: E402
from support import main  # noqa: E402

_OUT: dict = {}


def _fixture() -> tuple[Path, dict]:
    if not _OUT:
        d = Path(tempfile.mkdtemp())
        _OUT["dir"], _OUT["stats"] = d, site_fixture.build(d)
    return _OUT["dir"], _OUT["stats"]


def _json(name: str):
    return json.loads((_fixture()[0] / name).read_text(encoding="utf-8"))


def test_the_export_admin_and_team_steps_run_on_it():
    out, stats = _fixture()
    for name in ("stories.json", "briefing.json", "entities.json", "threads.json", "trackers.json", "work.json", "redirects.json",
                 "meta.json", "admin.json", "team.json", "primary-images.json", "episodes.json"):
        assert (out / name).exists(), name
    assert stats["export"]["stories"] == len(site_fixture.STORIES), stats["export"]
    assert stats["admin"] and stats["team"]["status"], stats
    assert _json("meta.json")["fixture"] == site_fixture.MARK


def test_it_holds_what_the_built_site_tests_look_for():
    stories = {s["id"]: s for s in _json("stories.json")}
    briefing = _json("briefing.json")
    assert len(briefing["storyIds"]) >= 5, briefing
    assert stories[briefing["storyIds"][0]]["slug"] == "northwind-labs-releases-atlas-3"
    # One story with the lead's full text (test_site_sitemap: none of it may reach the story page).
    full = [a for s in stories.values() for a in s["articles"] if a.get("contentMd")]
    assert len(full) == 1 and any(len(line) > 80 for line in full[0]["contentMd"].splitlines())
    assert {s["category"] for s in stories.values()} >= {"models", "marketing", "policy", "business"}
    assert any(len(s["articles"]) == 1 for s in stories.values()), "a single-outlet story"
    assert _json("redirects.json") == [{"from": site_fixture.MERGED_SLUG, "to": "northwind-labs-releases-atlas-3"}]
    trackers = _json("trackers.json")
    assert {m["name"] for m in trackers["models"]} == {"Atlas 3", "Atlas 2 Mini"}, trackers["models"]
    assert [f["company"] for f in trackers["funding"]] == ["Quill Robotics"]
    assert [t["tool"] for t in _json("work.json")["tools"]] == ["Shopfront Ad Writer"]
    assert _json("threads.json")[0]["slug"] == "northwind-atlas-3"
    assert len(_json("team.json")["roles"]) == 6
    # Today's episode names two live stories and one with no page (test_site_smoke: /listen must not link it).
    named = [s["slug"] for s in _json("episodes.json")[0]["stories"]]
    assert named[-1] == site_fixture.GONE_SLUG and site_fixture.GONE_SLUG not in {s["slug"] for s in stories.values()}
    assert all(slug in {s["slug"] for s in stories.values()} for slug in named[:-1])


def test_every_address_in_it_is_made_up():
    for s in _json("stories.json"):
        for a in s["articles"]:
            host = a["url"].split("/")[2]
            assert host.endswith(".test"), a["url"]
    for src in _json("sources.json"):
        assert src["url"].split("/")[2].endswith(".test"), src


def test_it_never_replaces_a_real_export():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "stories.json").write_text("[]", encoding="utf-8")
        (out / "meta.json").write_text("{}", encoding="utf-8")
        assert site_fixture.main(["--out", str(out)]) == 2
        assert (out / "stories.json").read_text(encoding="utf-8") == "[]", "a real export was overwritten"
    assert site_fixture.is_fixture(_fixture()[0])


if __name__ == "__main__":
    main(globals())
