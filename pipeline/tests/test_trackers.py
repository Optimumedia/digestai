"""Models tracker rules: python tests/test_trackers.py (offline)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest.trackers import fold_versions, is_release, model_key  # noqa: E402


def rel(name, lab, availability="api"):
    return {"name": name, "lab": lab, "availability": availability}


def test_same_model_spelled_differently_shares_a_key():
    keys = {model_key("DeepSeek-V4.1-Flash", "DeepSeek AI"), model_key("V4.1 Flash", "Deepseek"),
            model_key("DeepSeek V4.1 Flash", "DeepSeek"), model_key("V4.1-Flash", "DeepSeek")}
    assert len(keys) == 1, keys
    assert model_key("GPT‑6 Astra", "OpenAI") == model_key("GPT-6 Astra", "OpenAI")  # non-breaking hyphen
    assert model_key("WeatherNext 3", "Google DeepMind") == model_key("WeatherNext 3", "Google")
    assert model_key("GPT-6", "OpenAI") != model_key("GPT-6 Astra", "OpenAI")
    assert model_key("NVIDIA-Nemotron-3-Nano-4B-GGUF", "NVIDIA") == model_key("Nemotron 3 Nano 4B", "NVIDIA")


def test_real_launches_are_kept():
    assert is_release(rel("DeepSeek-V4.1-Flash", "DeepSeek", "open_weights"), "DeepSeek releases V4.1-Flash with 1M context")
    assert is_release(rel("Fugu Max", "Sakana AI", ""), "Sakana AI releases Fugu Max and Fugu Ultra")
    assert is_release(rel("Marengo Embed 3.0", "TwelveLabs"), "Amazon Bedrock Adds Marengo 3.0 for Video Search")
    assert is_release(rel("SWE-2", "Cognition", "consumer"), "Cognition Releases SWE-2 Coding Model")


def test_mentions_comparisons_and_research_are_dropped():
    assert not is_release(rel("Claude Fable 5", "Anthropic"), "Anthropic: Claude used by state and criminal hackers")
    assert not is_release(rel("GPT-6", "OpenAI"), "AI agents breach security, hack firms and spark US pause bill")
    assert not is_release(rel("GPT-5.6 Luna, GPT-6 Astra", "OpenAI", "open_weights"), "GPT-5.6 Luna vs GPT-6 Astra: Is $1.20 More Worth It?")
    assert not is_release(rel("scTransMIL", "Tencent AI for Life Sciences Lab", "research"), "scTransMIL links single-cell transcriptomes")
    assert not is_release(rel("GLARE", "arXiv"), "New GLARE model improves meeting continuity")
    assert not is_release(rel("i-Fold", "research team of Liu et al.", ""), "i-Fold improves protein structure prediction")
    assert not is_release(rel("Latent-Attention Masked Autoencoders (LAMAE)", "unknown", "research"), "Latent-Attention Masked Autoencoders Boost")
    assert not is_release(rel("Virtual Cell Model for TNBC", "Westlake University", ""), "AI virtual cell model predicts effective drugs")


def test_unversioned_name_folds_into_the_versioned_launch():
    rows = {model_key("WeatherNext 3", "Google"): {"name": "WeatherNext 3", "sources": 2, "date": "2026-09-08"},
            model_key("WeatherNext", "Google"): {"name": "WeatherNext", "sources": 1, "date": "2026-09-10"},
            model_key("Muse", "Meta"): {"name": "Muse", "sources": 1, "date": "2026-09-11"},
            model_key("Muse Spark", "Meta"): {"name": "Muse Spark", "sources": 1, "date": "2026-09-08"}}
    out = fold_versions(rows)
    assert sorted(r["name"] for r in out.values()) == ["Muse", "Muse Spark", "WeatherNext 3"]
    assert next(r for r in out.values() if r["name"] == "WeatherNext 3")["sources"] == 3


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
