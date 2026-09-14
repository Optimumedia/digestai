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
