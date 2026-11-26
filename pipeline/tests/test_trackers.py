"""Models tracker rules: python tests/test_trackers.py (offline)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest.trackers import fold_all, fold_versions, full_name, is_release, model_key  # noqa: E402


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
    # The larger count, not the sum: the ledger keeps the folded row and the window brings both rows
    # back next run, so a sum grew by one every run and a one-source row soon read as confirmed.
    assert next(r for r in out.values() if r["name"] == "WeatherNext 3")["sources"] == 2


# ---- 26 Nov 2026 audit: the 150 launches /api/models.json listed, 24 of them wrong ----

def row(name, lab, date, headline, availability="open_weights", sources=1):
    return {"name": name, "lab": lab, "availability": availability, "kind": "llm", "date": date,
            "storySlug": headline.lower()[:30], "storyHeadline": headline, "sources": sources}


def test_copies_apps_cloud_listings_and_stories_that_use_a_model_are_not_launches():
    for r, head in [
        (rel("Qwen3.8-27B-NariQ", "Nari Labs", "open_weights"), "Nari Labs releases Qwen3.8-27B quantized models in 3-bit, 2.5-bit formats"),
        (rel("Qwen3.8-27B-SLQ", "The Kaitchup", "open_weights"), "The Kaitchup releases statistically-lossless Qwen3.8-27B GGUF models"),
        (rel("Ling 3.1 flash (quantized GGUF builds)", "AtomicChat", "open_weights"), "AtomicChat releases quantized Ling 3.1 flash models with 86–88% token accuracy"),
        (rel("Xing4.0-29B-A4B", "XingChen", "open_weights"), "Bartowski releases Xing4.0-29B-A4B quantized models for llama.cpp"),
        (rel("Canva Brush", "Canva", "consumer"), "Canva launches Brush AI tool for Pro subscribers"),
        (rel("Minos", "Leash Bio", "open_weights"), "Leash Bio releases Minos, an AI playground for lab-in-the-loop simulations"),
        (rel("Muse", "Meta", "consumer"), "Meta launches Muse AI agent in Mexico, first in Latin America"),
        (rel("Alexa+", "Amazon", "consumer"), "Amazon launches Alexa+ early access in Japan with generative AI for Prime members"),
        (rel("Dialog-RSN-1", "PolyAI"), "PolyAI Dialog-RSN-1 available on Amazon SageMaker JumpStart"),
        (rel("Pegasus 1.5", "TwelveLabs"), "TwelveLabs Pegasus 1.5 becomes available on Amazon Bedrock"),
        (rel("GPT-6 Astra", "OpenAI"), "OpenAI launches healthcare computer-use agents with GPT-6 Astra and Luna models"),
        (rel("GPT-6.2 Astra", "OpenAI"), "OpenAI rolls out GPT-6.2 Sol for ChatGPT Plus, Pro, and Enterprise users"),
    ]:
        assert not is_release(r, head), (r["name"], head)
    # Still launches: an agent model with an API, a draft model named after its base, a name the
    # headline shortens, and a story whose later headline names a sibling model while the article's
    # own title names this one.
    assert is_release(rel("Voxtral 3", "Mistral AI", "api"), "Mistral AI launches Voxtral 3 voice agent stack")
    assert is_release(rel("Qwen3.8-27B-DSpine-b16", "Tencent"), "Tencent releases DSpine speculative draft model for Qwen3.8-27B")
    assert is_release(rel("Samaya Sterling", "Samaya AI", ""), "Samaya AI releases Sterling, a 3B-active finance research agent model")
    assert is_release(rel("Gemini 4 Carbon", "Google", "consumer"),
                      "Google releases Gemini 4 Neon with one-million-token context window | Google launches Gemini 4 Carbon for cyber defenders")
    assert full_name("6.2 Astra", "OpenAI releases GPT-6.2 Astra; Microsoft unveils seven MAI models") == "GPT-6.2 Astra"
    assert full_name("Fable 5.2", "Anthropic releases Claude Fable 5.2 and Claude Mythos 5.2 models") == "Fable 5.2"


def test_one_model_under_two_makers_or_three_names_is_one_row():
    rows = [
        row("MAI Code 2", "MAI", "2026-11-17T09:00:00Z", "Microsoft unveils seven AI models, saying MAI Code 2 matches 98% of GPT-6.1 Sol", "consumer"),
        row("MAI Code 2", "Microsoft", "2026-11-17T10:00:00Z", "Microsoft launches seven MAI models including MAI Code 2 at Ignite 2026", "consumer", 2),
        row("Arete-27B", "NousResearch", "2026-11-06T09:00:00Z", "Nous Research releases Arete-27B"),
        row("Arete-27B", "Nous Research", "2026-11-07T09:00:00Z", "Nous Research releases Arete-27B"),
        row("Inkling 2 Small", "Thinking Machines", "2026-11-05T09:00:00Z", "Thinking Machines releases Inkling 2 Small, a 594B MoE model"),
        row("Inkling 2 Small", "Inkling", "2026-11-06T09:00:00Z", "Thinking Machines releases Inkling 2 Small, a 594B MoE model", "consumer"),
        row("Mistral Large 4 (Le Chonk)", "Mistral AI", "2026-10-29T09:00:00Z", "Mistral releases Mistral Large 4 (Le Chonk) on Hugging Face"),
        row("Mistral Large 4 (ML4)", "Mistral AI", "2026-10-29T10:00:00Z", "Mistral releases Mistral Large 4 (Le Chonk) on Hugging Face"),
        row("Le Chonk", "Mistral AI", "2026-10-29T11:00:00Z", "Mistral releases Mistral Large 4 (Le Chonk) on Hugging Face"),
        row("NVIDIA-Nemotron-3.5-Transcribe-30B-A4B", "NVIDIA", "2026-10-28T09:00:00Z", "NVIDIA releases open-weight Nemotron 3.5 Transcribe model"),
        row("Nemotron 3.5 Transcribe", "NVIDIA", "2026-10-28T10:00:00Z", "NVIDIA releases open-weight Nemotron 3.5 Transcribe model"),
        row("Nemotron 3.5 Super VL 120B-A12B", "NVIDIA", "2026-10-15T09:00:00Z", "NVIDIA releases Nemotron 3.5 Super VL and Bolt edge models"),
        row("Nemotron 3.5 Super VL 120B", "NVIDIA", "2026-10-16T09:00:00Z", "NVIDIA releases Nemotron 3.5 Super VL and Bolt edge models"),
        row("DeepSeek-V4.1-Flash-20261017", "DeepSeek", "2026-10-18T09:00:00Z", "DeepSeek releases V4.1-Flash-1017 update with lower cost"),
        row("Deepseek V4.1 Flash (revision 1017)", "DeepSeek", "2026-10-23T09:00:00Z", "Deepseek launches V4.1 Flash 1017 model with 1 M token context"),
        row("Phonon 31m", "Gradium", "2026-10-22T09:00:00Z", "Gradium open-sources Phonon 31M on-device TTS model under Apache 2.0"),
        row("Phonon", "Gradium", "2026-11-05T09:00:00Z", "Hugging Face releases Phonon TTS with on-device speech and open weights"),
        row("WeVisOne-VL-4B", "Tencent", "2026-10-23T09:00:00Z", "Tencent releases WeVisOne-VL 4B vision-language model on Hugging Face"),
        row("WeVisOne-VL", "Tencent", "2026-11-04T09:00:00Z", "Tencent releases WeVisOne-VL, a 2B/4B open vision model"),
        row("6.2 Astra", "OpenAI", "2026-11-19T09:00:00Z", "OpenAI releases GPT-6.2 Astra; Microsoft unveils seven MAI models", "api"),
        row("GPT-6.2 Astra", "OpenAI", "2026-11-17T09:00:00Z", "OpenAI releases GPT-6.2 Astra", "api"),
        # Different models stay apart: sizes released separately, siblings, versions.
        row("Hy4", "Tencent", "2026-11-04T09:00:00Z", "Tencent releases Hy4"),
        row("Hy4 Flash", "Tencent", "2026-11-16T09:00:00Z", "Tencent launches Hy4 Flash"),
        row("Ming-3.0-audio-tiny", "inclusionAI", "2026-10-19T09:00:00Z", "InclusionAI releases Ming-3.0-audio models"),
        row("Ming-3.0-audio-flash", "inclusionAI", "2026-10-19T09:00:00Z", "InclusionAI releases Ming-3.0-audio models"),
        row("Gemma 4 2B", "Google", "2026-10-01T09:00:00Z", "Google releases Gemma 4 2B"),
        row("Gemma 4 27B", "Google", "2026-10-09T09:00:00Z", "Google releases Gemma 4 27B"),
        row("d1-ultra", "Liquid AI", "2026-10-16T09:00:00Z", "Liquid AI releases d1-ultra decision model"),
        row("d1.2-ultra", "Liquid AI", "2026-11-17T09:00:00Z", "Liquid AI releases d1.2 and d1.2-ultra decision models"),
        row("Muse Ember", "Meta", "2026-11-09T09:00:00Z", "Meta launches Muse Ember"),
        row("Prism", "Sarvam AI", "2026-11-04T09:00:00Z", "Sarvam AI releases open-weight Prism model"),
        row("Prism", "OpenAI", "2026-11-05T09:00:00Z", "OpenAI launches Prism"),
    ]
    keyed = {}
    for r in rows:
        k = model_key(r["name"], r["lab"])
        keyed.setdefault(k, r)  # the export joins same-key rows itself; none share a key here
    out = fold_all(keyed)
    got = sorted((r["name"], r["lab"], r["date"][:10]) for r in out.values())
    assert got == sorted([
        ("MAI Code 2", "Microsoft", "2026-11-17"),
        ("Arete-27B", "NousResearch", "2026-11-06"),
        ("Inkling 2 Small", "Thinking Machines", "2026-11-05"),
        ("Mistral Large 4", "Mistral AI", "2026-10-29"),
        ("Nemotron 3.5 Transcribe", "NVIDIA", "2026-10-28"),
        ("Nemotron 3.5 Super VL 120B", "NVIDIA", "2026-10-15"),
        ("Deepseek V4.1 Flash", "DeepSeek", "2026-10-18"),
        ("Phonon", "Gradium", "2026-10-22"),
        ("WeVisOne-VL", "Tencent", "2026-10-23"),
        ("GPT-6.2 Astra", "OpenAI", "2026-11-17"),
        ("Hy4", "Tencent", "2026-11-04"), ("Hy4 Flash", "Tencent", "2026-11-16"),
        ("Ming-3.0-audio-tiny", "inclusionAI", "2026-10-19"), ("Ming-3.0-audio-flash", "inclusionAI", "2026-10-19"),
        ("Gemma 4 2B", "Google", "2026-10-01"), ("Gemma 4 27B", "Google", "2026-10-09"),
        ("d1-ultra", "Liquid AI", "2026-10-16"), ("d1.2-ultra", "Liquid AI", "2026-11-17"),
        ("Muse Ember", "Meta", "2026-11-09"),
        ("Prism", "Sarvam AI", "2026-11-04"), ("Prism", "OpenAI", "2026-11-05"),
    ]), got
    # The kept row is the launch: the earliest report's story, the larger source count.
    mai = next(r for r in out.values() if r["name"] == "MAI Code 2")
    assert mai["sources"] == 2 and mai["date"] == "2026-11-17T09:00:00Z", mai
    assert model_key("GPT-6 Cyber", "OpenAI Group PBC") == model_key("GPT-6 Cyber", "OpenAI")


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
