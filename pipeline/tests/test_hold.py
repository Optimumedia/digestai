"""Risky-claim hold: python tests/test_hold.py (offline, no database)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest.hold import assess, decrypt, encrypt, find_risks, independent_sources, names_someone, registrable, write_review  # noqa: E402

ONE = ["theverge.com"]

RISKY_HEADLINES = [
    "Anthropic builds predictive surveillance system to monitor AI critics",
    "Houthis used Claude for ballistic missile development",
    "Anthropic reveals Houthis used Claude for ballistic missile development",
    "OpenAI sued by authors over training data",
    "Authors are suing Meta over pirated books",
    "xAI accused of spying on employees' Slack messages",
    "Former Google engineer charged with stealing AI trade secrets",
    "Hackers breach Scale AI customer database",
    "Meta failed to catch hundreds of AI child abuse ads",
    "Rain AI founder arrested over $50M fraud",
    "Clearview AI fined for illegal facial recognition database",
    "Chinese state hackers used Gemini to plan cyberattacks",
    "Nvidia chips smuggled to sanctioned militants, report alleges",
    "Character.AI faces lawsuit after teen suicide",
    "Whistleblower says Microsoft misled investors on AI revenue",
    "Palantir tool used to track journalists, documents show",
    "Tesla engineers allegedly harassed staff who raised Autopilot concerns",
    "FTC probe targets Amazon's Alexa voice recordings",
    "Perplexity scraped sites without permission, Cloudflare says",
]

ROUTINE_HEADLINES = [
    "OpenAI launches GPT-6 with a million-token context window",
    "Mistral raises $600M Series C led by Andreessen Horowitz",
    "Google DeepMind releases Gemma 4 open weights",
    "Nvidia unveils Blackwell successor at GTC",
    "Anthropic's Claude gets a memory feature for Pro users",
    "Hugging Face hackathon draws 5,000 builders",
    "Meta's Llama 5 tops coding benchmarks",
    "Microsoft adds data protection controls to Copilot",
    "Researchers probe how transformers store facts",
    "New robot hand improves dexterous manipulation",
    "Cohere signs enterprise deal with Oracle",
    "Perplexity valued at $20 billion in new funding round",
    "Import AI 472: DeepMind's reward hacking results",
    "Show HN: open-source agent framework trends on Hacker News",
    "Apple acquires AI startup for on-device models",
    "Study finds chatbots can help plan bioweapons",  # no named person or company
]


def test_examples_from_the_site_are_held():
    for h in RISKY_HEADLINES[:3]:
        assert assess(h, [], "", {}, ONE), h


def test_risky_single_source_headlines_are_held():
    missed = [h for h in RISKY_HEADLINES if not assess(h, [], "", {}, ONE)]
    assert not missed, missed


def test_routine_news_is_not_held():
    wrong = [(h, assess(h, [], "", {}, ONE)) for h in ROUTINE_HEADLINES]
    wrong = [w for w in wrong if w[1]]
    assert not wrong, wrong


def test_second_independent_source_releases_the_hold():
    h = RISKY_HEADLINES[1]
    assert assess(h, [], "", {}, ["techcrunch.com", "news.techcrunch.com"])  # one publisher, two hosts
    assert assess(h, [], "", {}, ["www.theverge.com", "theverge.com"])
    assert assess(h, [], "", {}, ["theverge.com", "wired.com"]) is None


def test_body_needs_two_different_risky_terms():
    head = "OpenAI ships new reasoning model"
    passing = ["The model is faster.", "OpenAI also faces a lawsuit from authors."]
    assert assess(head, passing, "Prices drop by half.", {"companies": ["OpenAI"]}, ONE) is None
    heavy = ["Documents allege the company spied on rival researchers.", "Two staff were fired."]
    reason = assess(head, heavy, "", {"companies": ["OpenAI"]}, ONE)
    assert reason and "summary" in reason


def test_verb_forms_and_whole_words():
    terms = lambda t: {x for _, x in find_risks(t)}  # noqa: E731
    assert {"sues", "sued", "suing"} <= terms("X sues Y. Z sued W. V is suing U.")
    assert not terms("A new suite of tools; a hackathon; the model's refined output; defined roles")
    assert "surveillance" in terms("predictive surveillance system")
    assert "hacked" in terms("Company hacked") and not terms("Discussed on Hacker News")


def test_named_party_detection():
    assert names_someone("Anthropic builds predictive surveillance system")
    assert names_someone("Houthis used Claude for missile development")
    assert not names_someone("Study finds chatbots can help plan bioweapons")
    assert not names_someone("Startup founder arrested over $50M fraud")  # "$50M" is not a name
    assert names_someone("xAI accused of spying")
    assert names_someone("study finds chatbots help plan attacks", {"companies": ["OpenAI"]})


def test_reason_names_category_and_terms():
    reason = assess(RISKY_HEADLINES[1], [], "", {}, ONE)
    assert reason.startswith("Single source; headline mentions weapons or military use"), reason
    assert '"ballistic"' in reason


def test_registrable_domains():
    assert registrable("www.bbc.co.uk") == registrable("news.bbc.co.uk") == "bbc.co.uk"
    assert registrable("blog.openai.com") == "openai.com"
    assert independent_sources(["a.com", "www.a.com", "b.org"]) == 2


def test_encryption_round_trip_and_public_count():
    payload = {"generatedAt": "2026-09-14T10:00:00Z", "stories": [{"slug": "x", "headline": "Houthis used Claude"}]}
    blob = encrypt(payload, "test passphrase", iterations=1000)
    assert set(blob) >= {"v", "salt", "iv", "data"}
    assert "Houthis" not in json.dumps(blob)
    assert decrypt(blob, "test passphrase") == payload
    try:
        decrypt(blob, "wrong")
        raise AssertionError("wrong key decrypted")
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - InvalidTag
        pass
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "held.enc.json").write_text("stale", encoding="utf-8")
        assert write_review(out, payload["stories"], payload["generatedAt"], passphrase="") == {"encrypted": False}
        assert json.loads((out / "held.json").read_text(encoding="utf-8")) == {"count": 1, "updatedAt": payload["generatedAt"]}
        assert not (out / "held.enc.json").exists()


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
