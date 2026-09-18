"""Google Discover readiness: headline rules and share-card sizes. python tests/test_discover.py (offline)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import checks, images  # noqa: E402
from digest.checks import discipline_headline, real_question, usable_headline  # noqa: E402
from digest.enrich import _clean  # noqa: E402

# Headlines as they are on the site today (site/src/data, the live RSS feed, 18 Sep 2026): the rules
# must leave every one of them alone.
REAL = [
    "OpenAI launches Agents API for managed, sandboxed AI workflows",
    "Cognition’s SWE‑2 model hits 92.8 on Terminal‑Bench 2.1, topping published scores",
    "The Waymo Effect: How AI is Quietly Driving 'Decollaboration' in Academic Research",
    "Meta Unveils $100-a-Month AI Agent: Muse",
    "OpenAI Unveils GPT‑6 Astra: Record‑Breaking 3D Rendering, Loop‑Transformer Architecture",
    "META Stock Surges 10% After Muse AI Agent Launch",
    "Opinion: When Does a Business Become AI‑Native, Not Just AI‑Enabled?",
    "Is Trump’s AI obsession walking the world into disaster?",
    "Could AI really end humanity? Guardian invites questions",
    "AI developer vibe codes DLSS 5 onto Intel CPU's integrated graphics —Intel Arc 140T runs neural rendering in 360p at 10 frames per second",
    "SK hynix launches SK hynix Ventures CVC brand to back AI ecosystem",
    "Salesforce & AWS Expand AI Integrations",
    "Six AI Training Courses Bundle for $29.99",
    "Anthropic reportedly eyeing $2 trillion IPO as CEO warns of real AI dangers",
    "Speculative decoding explained: how AI speeds up text generation",
]


def test_real_headlines_are_left_alone():
    for h in REAL:
        out, changed = discipline_headline(h)
        assert out == h and changed == [], (h, out, changed)


def test_hype_words_are_replaced():
    assert discipline_headline("Google Unleashes Gemini 4 in Chrome")[0] == "Google Releases Gemini 4 in Chrome"
    assert discipline_headline("Revolutionizing search: Google adds Gemini 4 to Chrome")[0] == "Changing search: Google adds Gemini 4 to Chrome"
    assert discipline_headline("Meta's Muse is a game-changer for ad agencies")[0] == "Meta's Muse is a major shift for ad agencies"
    # A dropped adjective fixes the article in front of it.
    assert discipline_headline("Mistral ships a game-changing AI model for coding")[0] == "Mistral ships an AI model for coding"
    assert discipline_headline("Game-Changing robot arm from Figure lifts 40 kg")[0] == "Robot arm from Figure lifts 40 kg"


def test_clickbait_frames_are_removed():
    assert discipline_headline("You won't believe what GPT-6 can do with spreadsheets")[0] == "What GPT-6 can do with spreadsheets"
    assert discipline_headline("Everything you need to know about GPT-6")[0] == "What we know about GPT-6"
    assert discipline_headline("GPT-6 launches in Europe: everything you need to know")[0] == "GPT-6 launches in Europe"
    assert discipline_headline("Nvidia stock falls 5%. Here's why")[0] == "Nvidia stock falls 5%"
    assert discipline_headline("Nvidia stock falls 5% and here's why it matters")[0] == "Nvidia stock falls 5%"
    assert discipline_headline("Here's why Apple is delaying Siri AI")[0] == "Why Apple is delaying Siri AI"
    assert discipline_headline("BREAKING: Meta releases Llama 5 weights")[0] == "Meta releases Llama 5 weights"
    # A sentence that merely contains the words is not a frame.
    assert discipline_headline("Study shows this is how models learn")[1] == []


def test_exclamation_marks_and_capitals():
    assert discipline_headline("GPT-6 is here!!! And it's INCREDIBLE")[0] == "GPT-6 is here. And it's incredible"
    assert discipline_headline("Meta releases HUGE new Llama model")[0] == "Meta releases huge new Llama model"
    assert discipline_headline("Nvidia Posts MASSIVE Quarter on Data Center Sales")[0] == "Nvidia Posts Massive Quarter on Data Center Sales"
    assert discipline_headline("Yahoo! adds AI answers to search")[1] == []  # a brand's own name
    for keep in ("NASA and IBM release Prithvi weather model", "NVIDIA ships H200 NVL to AWS", "EU AI Act fines start for GPAI providers"):
        assert discipline_headline(keep)[1] == [], keep


def test_colons():
    out, changed = discipline_headline("Mistral Large 3: specs: pricing and availability")
    assert out == "Mistral Large 3: specs — pricing and availability" and "extra colons replaced" in changed
    assert discipline_headline("Opinion: AI labs: who audits the auditors")[1] == []  # the label does not count
    assert discipline_headline("OpenAI moves launch to 10:30 GMT: what changes")[1] == []  # a time is not a colon


def test_question_marks():
    # A statement with a question mark, and the source states it: the mark goes.
    assert discipline_headline("OpenAI launches GPT-6?", "OpenAI launches GPT-6 with a 2M context")[0] == "OpenAI launches GPT-6"
    # The source hedges: the question mark is the hedge and stays.
    assert discipline_headline("OpenAI to launch GPT-6?", "OpenAI may launch GPT-6 next week")[1] == []
    assert discipline_headline("OpenAI to launch GPT-6?", "Will OpenAI launch GPT-6?")[1] == []
    # No source title to compare with: never turn a question into a fact.
    assert discipline_headline("OpenAI to launch GPT-6?")[1] == []
    # A real question is a question.
    assert discipline_headline("Is Anthropic's new model safer than GPT-6?", "Anthropic ships Claude 6")[1] == []
    assert real_question("Nvidia's new chip: can it beat AMD?")
    assert real_question("Opinion: Why does every lab ship an agent?")
    assert not real_question("OpenAI launches GPT-6?")


def test_never_empty_or_broken():
    # Nothing left after the rules: the source's own title, cleaned the same way.
    out, changed = discipline_headline("You won't believe this!!!", "Google adds Gemini 4 to Chrome for US users!")
    assert out == "Google adds Gemini 4 to Chrome for US users"
    assert changed == ["headline replaced with the source's own title"]
    # No usable title either: the headline exactly as written.
    assert discipline_headline("You won't believe this!!!", "WOW!!!") == ("You won't believe this!!!", [])
    assert discipline_headline("You won't believe this!!!") == ("You won't believe this!!!", [])
    assert discipline_headline("") == ("", [])
    assert discipline_headline(None) == ("", [])
    for bad in ("", "Hi", "OpenAI:", "GPT-6 (beta", "“Quoted headline without end"):
        assert not usable_headline(bad), bad
    assert usable_headline("OpenAI launches GPT-6 (beta)")


def test_rules_are_idempotent():
    for h in ["BREAKING: Google Unleashes Gemini 4!!! Here's why", "Mistral ships a game-changing AI model: specs: price",
              "Everything you need to know about GPT-6", *REAL]:
        once = discipline_headline(h, "Google adds Gemini 4 to Chrome")[0]
        assert discipline_headline(once, "Google adds Gemini 4 to Chrome")[0] == once, (h, once)


def test_enrich_applies_the_rules():
    row = SimpleNamespace(id=1, title="Google adds Gemini 4 to Chrome")
    out = _clean({"headline": "Google Unleashes Gemini 4 in Chrome!", "summary_md": "x"}, row, None)
    assert out["headline"] == "Google Releases Gemini 4 in Chrome"
    assert sorted(out["headline_rules"]) == ["exclamation mark removed", "hype word replaced"]
    # The fallback to the source title in checks.safer() is cleaned too.
    bad = {"headline": "Mistral raises 4 billion euros", "summary_md": "", "key_points": [], "why_it_matters": "", "entities": {}}
    rep = {"figures": ["4 billion"], "names": []}
    safe, _ = checks.safer(bad, rep, SimpleNamespace(id=1, title="Mistral raises 3 billion euros!!"), "")
    assert safe["headline"] == "Mistral raises 3 billion euros"


def test_prompts_ask_for_plain_headlines():
    from digest import enrich, upgrade

    for text in (enrich.SCHEMA, upgrade.MULTI_PROMPT):
        assert "subject, verb, object" in text and "under 90 characters" in text and "here's why" in text


# ---------------------------------------------------------------------------- share cards

def test_card_and_variants_are_at_least_1200_wide():
    from PIL import Image

    story = {"slug": "s", "headline": "Cognition’s SWE‑2 model hits 92.8 on Terminal‑Bench 2.1, topping published scores and a very long tail that must wrap",
             "category": "models", "categoryName": "Generative AI & Models", "keyPoints": ["SWE-2 scores 92.8"], "articleCount": 2,
             "coverage": {"primary": 1}, "firstPublishedAt": "2026-09-18T10:00:00Z"}
    with tempfile.TemporaryDirectory() as tmp:
        images.render(story, Path(tmp) / "card.png")
        assert Image.open(Path(tmp) / "card.png").size == (1200, 630)
        for key, size in images.VARIANTS.items():
            p = Path(tmp) / f"card-{key}.png"
            images.render(story, p, size)
            assert Image.open(p).size == size and size[0] >= 1200
    assert {k: round(w / h, 2) for k, (w, h) in images.VARIANTS.items()} == {"16x9": 1.78, "4x3": 1.33, "1x1": 1.0}


def test_card_lines_fit_the_card():
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = images._font("Newsreader.ttf", 76, 500, 72)
    lines = images._wrap(d, "System76 Launches Thelio Mira AI Linux Workstation with Up to 192 GB GPU Memory", font, 1040)
    assert all(d.textlength(line, font=font) <= 1040 for line in lines), lines
    assert images._wrap(d, "SWE‑2 on Terminal‑Bench", font, 1040) == ["SWE-2 on Terminal-Bench"]  # no missing glyphs


def test_variants_are_drawn_once_and_kept_by_the_prune():
    story = {"slug": "fresh", "headline": "OpenAI launches Agents API", "category": "agents", "keyPoints": [], "articleCount": 1}
    old_out = images.OUT
    with tempfile.TemporaryDirectory() as tmp:
        images.OUT = Path(tmp)
        try:
            recent: set = set()
            assert images.render_variants(story, recent, budget=2) == 2
            assert images.render_variants(story, recent, budget=5) == 1
            assert images.render_variants(story, recent, budget=5) == 0
            assert recent == {"fresh-16x9.png", "fresh-4x3.png", "fresh-1x1.png"}
        finally:
            images.OUT = old_out


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
