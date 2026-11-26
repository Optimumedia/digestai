"""Evergreen search pages: python tests/test_site_evergreen.py

The rules in site/src/lib/evergreen.mjs (run in Node) behind /models/pricing, /models/open-weights,
/models/context-windows, /compare/<a>-vs-<b> and /work/tools/<task>: a price is taken only when one
sentence names one model and states its list price, lab spellings and duplicate names collapse, a
comparison needs shared stories, shared facts and a sentence that compares the two, and every page
has a threshold below which it is noindex (or, for a model pair, not built). When the site has been
built, also that the built pages and the sitemap agree with those rules."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"


def _run(script: str) -> dict:
    lib = (SITE / "src" / "lib" / "evergreen.mjs").as_uri()
    probe = f"import * as eg from {json.dumps(lib)};\n{script}"
    res = subprocess.run([shutil.which("node"), "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:800]
    return json.loads(res.stdout)


def _node() -> bool:
    if not shutil.which("node"):
        print("SKIP node not installed")
        return False
    return True


def test_prices_only_from_a_plain_statement():
    if not _node():
        return
    cases = {
        "labelled": "OpenAI launched GPT-6.1 Sol, priced at $2 per million input tokens and $10 per million output tokens.",
        "shorthand": "Gemini 4 Argon launches with introductory pricing of $2/$10 per million input/output tokens.",
        "input_only": "Clairic is priced at $0.10 per million input tokens.",
        "cached": "Cached input costs $0.10 per million tokens, 95% cheaper than standard input for GPT-6.1 Sol.",
        "fast_mode": "Opus 5.5 fast mode doubles pricing to $8/$40 per million tokens.",
        "change": "Sol's input price fell from $4 to $2 per million tokens and its output price from $20 to $10 per million tokens.",
        "match": "Claude Haiku 5.5 matches GPT-6 Luna's pricing at $0.10/$0.50 per million.",
        "tiered": "Pricing is $0.10 per million input tokens and $0.50 per million output tokens for prompts under 100,000 tokens.",
        "discount": "Launch pricing: $0.10/M input, $0.35/M output tokens (50% off).",
        "vs": "Opus 5.5 priced at $4 input / $20 output per 1M tokens (vs. Fable's $10 / $50).",
        "no_unit": "The plan costs $20 a month.",
    }
    out = _run(f"const c = {json.dumps(cases)};\nconsole.log(JSON.stringify(Object.fromEntries(Object.entries(c).map(([k, v]) => [k, eg.priceFromSentence(v)]))));")
    assert out["labelled"] == {"input": 2, "output": 10}
    assert out["shorthand"] == {"input": 2, "output": 10}
    assert out["input_only"] == {"input": 0.1, "output": None}
    assert out["vs"] == {"input": 4, "output": 20}  # the subject's own price, before the comparison
    for k in ("cached", "fast_mode", "change", "match", "tiered", "discount", "no_unit"):
        assert out[k] is None, (k, out[k])


def test_models_collapse_and_prices_go_to_the_right_model():
    if not _node():
        return
    models = [
        {"name": "Claude Opus 5.5", "lab": "Claude", "kind": "multimodal", "availability": "consumer", "context": "1M tokens", "date": "2026-09-21T00:00:00Z", "storySlug": "opus"},
        {"name": "Opus 5.5", "lab": "Anthropic", "kind": "llm", "availability": "api", "date": "2026-09-22T00:00:00Z", "storySlug": "opus2"},
        {"name": "GPT-6 Sol", "lab": "Microsoft", "kind": "llm", "availability": "api", "context": "1,050,000 tokens", "date": "2026-09-10T00:00:00Z", "storySlug": "sol"},
        {"name": "Glimpse", "lab": "Elorian", "kind": "multimodal", "availability": "open_weights", "license": "Apache 2.0", "context": "2048 positions", "date": "2026-10-16T00:00:00Z", "storySlug": "glimpse"},
    ]
    stories = [
        {"id": 1, "slug": "opus", "headline": "Anthropic launches Claude Opus 5.5", "articles": [{}], "firstPublishedAt": "2026-09-21T00:00:00Z",
         "summaryMd": "Claude Opus 5.5 costs $4 per million input tokens and $20 per million output tokens.", "keyPoints": []},
        {"id": 2, "slug": "glimpse", "headline": "Elorian releases Glimpse, a visual reasoning model", "articles": [{}], "firstPublishedAt": "2026-10-16T00:00:00Z",
         "summaryMd": "The model costs $0.60 per million input tokens and $3 per million output tokens.", "keyPoints": []},
        {"id": 3, "slug": "roundup", "headline": "Model prices this week", "articles": [{}], "firstPublishedAt": "2026-10-17T00:00:00Z",
         "summaryMd": "The model costs $9 per million input tokens and $90 per million output tokens.", "keyPoints": []},
    ]
    out = _run(
        f"const g = eg.buildEvergreen({{stories: {json.dumps(stories)}, models: {json.dumps(models)}, tools: []}});\n"
        "console.log(JSON.stringify({canon: g.canon.map((m) => [m.slug, m.lab, m.names, m.contextTokens, m.availability]),"
        " prices: Object.fromEntries([...g.prices].map(([k, v]) => [k, [v.input, v.output, v.slug]]))}));")
    canon = {c[0]: c for c in out["canon"]}
    assert set(canon) == {"claude-opus-5-5", "gpt-6-sol", "glimpse"}       # "Opus 5.5" joined the longer name
    assert canon["claude-opus-5-5"][1] == "Anthropic" and canon["gpt-6-sol"][1] == "OpenAI"
    assert canon["claude-opus-5-5"][2] == ["Claude Opus 5.5", "Opus 5.5"]
    assert canon["claude-opus-5-5"][4] == ["api", "consumer"]
    assert canon["gpt-6-sol"][3] == 1050000 and canon["glimpse"][3] is None  # positions are not a context window
    assert out["prices"]["claude-opus-5-5"] == [4, 20, "opus"]
    assert out["prices"]["glimpse"] == [0.6, 3, "glimpse"]   # its own launch story, which names it
    assert len(out["prices"]) == 2                          # "the model" in a round-up belongs to no one


def test_a_price_is_not_given_to_a_model_it_does_not_belong_to():
    """26 Nov 2026, /models/pricing: GLM-Legal showed its base model's price, Gemini 4 Hydrogen showed
    Neon's, and the Decisions API (not a model) had a row."""
    if not _node():
        return
    models = [
        {"name": "GLM-Legal", "lab": "Z.ai", "kind": "llm", "availability": "api", "date": "2026-10-13T00:00:00Z", "storySlug": "glm"},
        {"name": "Gemini 4 Hydrogen", "lab": "Google", "kind": "llm", "availability": "api", "date": "2026-11-10T00:00:00Z", "storySlug": "hydrogen"},
        {"name": "Decisions API", "lab": "OpenAI", "kind": "other", "availability": "api", "date": "2026-11-15T00:00:00Z", "storySlug": "decisions"},
        {"name": "d1-ultra", "lab": "Liquid AI", "kind": "llm", "availability": "api", "date": "2026-10-16T00:00:00Z", "storySlug": "d1"},
        {"name": "Step 5 Preview", "lab": "StepFun", "kind": "llm", "availability": "open_weights", "date": "2026-10-17T00:00:00Z", "storySlug": "step"},
    ]
    def st(i, slug, headline, summary, at):
        return {"id": i, "slug": slug, "headline": headline, "articles": [{}], "firstPublishedAt": at, "summaryMd": summary, "keyPoints": []}
    stories = [
        st(1, "glm", "Z.ai previews GLM-Legal for law firms",
           "Developers can prototype using the base model ($0.15/$0.50 per 1M tokens) before swapping to GLM-Legal.", "2026-10-13T00:00:00Z"),
        st(2, "hydrogen", "Google releases Gemini 4 Hydrogen in AI Studio",
           "Gemini 4 Hydrogen is free in AI Studio; Neon costs $3.00/$15.00 per 1M tokens, both offer four reasoning depths.", "2026-11-23T00:00:00Z"),
        st(3, "decisions", "OpenAI makes its Decisions API widely available",
           "OpenAI released a Decisions API in public beta on October 6 at $0.10 per million input tokens.", "2026-11-15T00:00:00Z"),
        # Still the launched model's own price: a plain statement in its launch story.
        st(4, "d1", "Liquid AI releases d1-ultra decision model",
           "It is currently available via the Liquid API at a rate of $0.042 per million input tokens.", "2026-10-16T00:00:00Z"),
        st(5, "step", "StepFun releases Step 5 Preview open weights",
           "The FP8 checkpoint requires 4x B300 or 8x H200 GPUs, and the API charges $1.00 per 1M input tokens and $2.70 per 1M output tokens.", "2026-10-17T00:00:00Z"),
    ]
    out = _run(
        f"const g = eg.buildEvergreen({{stories: {json.dumps(stories)}, models: {json.dumps(models)}, tools: []}});\n"
        "console.log(JSON.stringify(Object.fromEntries([...g.prices].map(([k, v]) => [k, [v.input, v.output]]))));")
    assert out == {"d1-ultra": [0.042, None], "step-5-preview": [1, 2.7]}, out


def test_context_windows_parse():
    if not _node():
        return
    texts = ["1.05M tokens", "262,144 tokens", "one million tokens", "500K token context window", "65536", "2048 positions", "", "16,384 tokens (262k on dedicated)"]
    out = _run(f"console.log(JSON.stringify({json.dumps(texts)}.map(eg.parseContext)));")
    assert out == [1050000, 262144, 1000000, 500000, 65536, None, None, 16384]


def test_pairs_and_tasks_follow_their_thresholds():
    if not _node():
        return
    models = [
        {"name": "Alpha 2", "lab": "A Labs", "kind": "llm", "availability": "api", "context": "1M tokens", "date": "2026-09-01T00:00:00Z", "storySlug": "s1"},
        {"name": "Beta 3", "lab": "B Labs", "kind": "llm", "availability": "open_weights", "license": "MIT", "context": "256K tokens", "date": "2026-09-05T00:00:00Z", "storySlug": "s2"},
        {"name": "Gamma 1", "lab": "C Labs", "kind": "llm", "availability": "api", "context": "128K tokens", "date": "2026-09-06T00:00:00Z", "storySlug": "s9"},
    ]

    def story(i, text, names):
        return {"id": i, "slug": f"s{i}", "headline": text, "articles": [{}], "articleCount": 2, "firstPublishedAt": f"2026-09-{i:02d}T00:00:00Z",
                "summaryMd": text, "keyPoints": [], "entities": {"models": names}}
    stories = [
        story(1, "Alpha 2 is faster than Beta 3 on coding tests.", ["Alpha 2", "Beta 3"]),
        story(2, "Beta 3 opens its weights.", ["Beta 3", "Alpha 2"]),
        story(3, "Alpha 2 and Beta 3 both top a leaderboard.", ["Alpha 2", "Beta 3"]),
        story(4, "Gamma 1 trails Alpha 2.", ["Gamma 1", "Alpha 2"]),
        story(9, "Gamma 1 launches.", ["Gamma 1"]),
    ]
    tool = lambda name, what, kind="free", maker="Acme", link="https://acme.example": {
        "key": name.lower(), "tool": name, "maker": maker, "whatItDoes": what, "whoFor": [], "jobs": [], "cost": None, "costKind": kind,
        "effort": "minutes", "watchOut": "", "link": link, "lastChange": "2026-10-01T00:00:00Z", "firstSeen": None, "changes": [], "changeCount": 0}
    tools = [tool(f"Rank{i}", "Tracks SEO rankings in AI search") for i in range(6)] + [
        tool("Mailer", "Writes email newsletters"), tool("u/someone", "Writes SEO briefs", maker="u/someone"),
        tool("seo.js", "An SEO script"), tool("Alpha 2", "Answers SEO questions")]
    out = _run(
        f"const g = eg.buildEvergreen({{stories: {json.dumps(stories)}, models: {json.dumps(models)}, tools: {json.dumps(tools)}}});\n"
        "console.log(JSON.stringify({pairs: g.pairs.filter((p) => p.kind === 'model').map((p) => [p.slug, p.shared.length, p.quotes.length]),"
        " assistants: g.pairs.filter((p) => p.kind === 'assistant').map((p) => [p.slug, p.indexable]),"
        " seo: g.tasks.find((t) => t.slug === 'seo').tools.map((t) => t.tool), seoIdx: g.tasks.find((t) => t.slug === 'seo').indexable,"
        " email: g.tasks.find((t) => t.slug === 'email').indexable, pages: Object.fromEntries([...g.pages].map(([k, v]) => [k, v.indexable]))}));")
    # Alpha 2 and Beta 3 share three stories, four facts and a sentence comparing just the two; Gamma 1
    # shares one story with Alpha 2: not built.
    assert out["pairs"] == [["alpha-2-vs-beta-3", 3, 1]], out["pairs"]
    assert out["pages"]["/compare/alpha-2-vs-beta-3"] is True
    assert not any("gamma" in p for p in out["pages"])
    # Assistant pairs are always built, and noindex without the coverage behind them.
    assert len(out["assistants"]) == 10 and not any(ix for _s, ix in out["assistants"])
    # Tasks: a forum handle, a script and a model are not tools; six tools make the page indexable.
    assert out["seo"] == [f"Rank{i}" for i in range(6)] and out["seoIdx"] is True
    assert out["email"] is False
    assert out["pages"]["/models/pricing"] is False and out["pages"]["/models/open-weights"] is False


def test_built_site():
    dist = SITE / "dist"
    if not (dist / "models" / "pricing.html").exists():
        print("SKIP site not built")
        return
    sitemap = "".join(p.read_text(encoding="utf-8") for p in dist.glob("sitemap-*.xml"))
    for page in [dist / "models" / "pricing.html", dist / "models" / "open-weights.html", dist / "models" / "context-windows.html",
                 dist / "compare.html", *sorted((dist / "compare").glob("*.html")), *sorted((dist / "work" / "tools").glob("*.html"))]:
        html = page.read_text(encoding="utf-8")
        path = "/" + page.relative_to(dist).as_posix().removesuffix(".html")
        noindex = 'content="noindex, follow"' in html
        listed = f"https://digestai.news{path}</loc>" in sitemap
        assert noindex != listed, (path, noindex, listed)       # the page and the sitemap agree
        assert "contentMd" not in html


def test_templates_keep_the_house_style():
    """No em dash in the pages' own copy (quoted digest sentences are the stories' text, unchanged)."""
    pages = [SITE / "src" / "pages" / p for p in ("models/pricing.astro", "models/open-weights.astro", "models/context-windows.astro",
                                                   "compare/index.astro", "compare/[pair].astro", "work/tools/[task].astro")]
    for page in pages + [SITE / "src" / "lib" / "evergreen.mjs"]:
        text = page.read_text(encoding="utf-8")
        copy = "\n".join(line for line in text.splitlines() if not line.strip().startswith(("/*", "*", "//")))
        assert "—" not in copy, page.name
    for page in pages:
        assert "thin={" in page.read_text(encoding="utf-8"), page.name   # every page carries its threshold


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
