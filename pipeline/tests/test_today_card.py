"""The /today share card (images.today_card): drawn from the export's files, once per set of top
stories, kept under a dated name. Offline: python tests/test_today_card.py"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import images  # noqa: E402

LONG = "Anthropic ships a model that matches its largest system on coding tests at a fifth of the price and puts it in the free tier of its chat app the same day for every user"


def write(d: Path, ids=(1, 2, 3), date="2026-10-30") -> None:
    stories = [{"id": i, "headline": LONG if i == 1 else f"Short headline number {i}"} for i in range(1, 7)]
    (d / "stories.json").write_text(json.dumps(stories), encoding="utf-8")
    (d / "briefing.json").write_text(json.dumps({"date": date, "storyIds": list(ids), "stats": {"articles": 412}}), encoding="utf-8")


def in_temp(fn):
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            out, images.OUT = images.OUT, d / "og"
            try:
                fn(d)
            finally:
                images.OUT = out
    run.__name__ = fn.__name__
    return run


@in_temp
def test_draws_a_share_sized_card_under_a_dated_name(d):
    write(d)
    assert images.today_card(d) is True
    path = images.today_path("2026-10-30")
    assert path.name == "today-2026-10-30.png"
    with Image.open(path) as img:
        assert img.size == (1200, 630)
    assert path.stat().st_size < 120_000


@in_temp
def test_draws_again_only_when_the_top_stories_change(d):
    write(d)
    assert images.today_card(d) is True
    assert images.today_card(d) is False
    write(d, ids=(2, 3, 4))
    assert images.today_card(d) is True
    assert len(list(images.OUT.glob("*.stamp"))) == 1


@in_temp
def test_too_few_stories_or_no_files_draw_nothing(d):
    assert images.today_card(d) is False
    write(d, ids=(1, 2))
    assert images.today_card(d) is False
    assert not images.OUT.exists() or not list(images.OUT.glob("*.png"))


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
