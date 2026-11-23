"""Pages and files for AI assistants: python tests/test_site_answers.py

The tracker answer rules (site/src/lib/answers.mjs, run in Node): what may be stated as a model
release, one spelling per lab, the first report date, the newest model per lab and the month pages.
robots.txt welcomes AI crawlers without opening /ft/, /search or /admin. When the site has been built:
the answer files exist, carry no publisher text, state their date, and thin month pages stay out of
the sitemap."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"
FORBIDDEN = {"contentMd", "content", "fullText", "full_text", "text_md", "body", "description", "articleBody"}
AI_AGENTS = ["OAI-SearchBot", "ChatGPT-User", "GPTBot", "Claude-SearchBot", "Claude-User", "ClaudeBot",
             "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot-Extended", "Amazonbot", "DuckAssistBot"]


def _run(script: str):
    lib = (SITE / "src" / "lib" / "answers.mjs").as_uri()
    probe = f"import * as a from {json.dumps(lib)};\n{script}"
    res = subprocess.run([shutil.which("node"), "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:800]
    return json.loads(res.stdout)


def row(name, lab, date, headline, kind="llm", sources=2, **extra):
    return {"name": name, "lab": lab, "kind": kind, "availability": "api", "date": date, "storySlug": name.lower().replace(" ", "-"),
            "storyHeadline": headline, "sources": sources, **extra}


def test_labs_have_one_spelling_and_placeholders_name_no_maker():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    out = _run("console.log(JSON.stringify(['Claude', 'Google DeepMind', 'SpaceXAI', 'Alibaba Qwen Team', 'Meta Superintelligence Labs', "
               "'Allen Institute for AI (Ai2)', 'TBC', 'Jialinyyzz (unspecified org)', 'the authors', 'NASA and IBM', 'Datalab', ''].map(a.canonicalLab)))")
    assert out == ["Anthropic", "Google", "xAI", "Alibaba", "Meta", "Allen Institute for AI", None, None, None, "NASA and IBM", "Datalab", None], out


def test_only_launches_are_stated_and_the_date_is_the_first_report():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    rows = [
        row("Opus 6", "Anthropic", "2026-10-14", "Anthropic may launch Opus 6 next week to rival OpenAI's Bell"),
        row("Decisions API", "OpenAI", "2026-10-22", "OpenAI makes its Decisions API widely available", kind="other"),
        row("Clairic", "Cloudflare", "2026-10-19", "Cloudflare releases Clairic, a System One extraction model", kind="other"),
        row("Claude Opus 5.5", "Claude", "2026-10-29", "Claude Opus 5.5 turns text prompts into motion graphics"),
        row("Claude Haiku 5.5", "Anthropic", "2026-10-07", "OpenAI and Anthropic cut AI model prices by up to 80%"),
        row("Claude Haiku 5.5", "Claude", "2026-10-24", "Claude Haiku 5.5: new features and pricing"),
        row("Deepseek V4.1 Flash (revision 1017)", "Deepseek", "2026-10-23", "Deepseek launches V4.1 Flash 1017 model with 1M token context"),
        row("Kumo Relational", "NVIDIA", "2026-10-21", "Nvidia releases Kumo Forecast time-series model"),
        row("Mischief", "TBC", "2026-10-30", "TBC launches Mischief video model"),
        row("GPT-6.1 Terra", "OpenAI", "2026-10-26", "OpenAI launches GPT-6.1 Terra for agents", sources=1),
    ]
    out = _run(f"""
const rows = a.withCanonicalLabs({json.dumps(rows)});
const list = a.releaseList(rows);
console.log(JSON.stringify({{
  issues: Object.fromEntries(rows.map((r) => [r.name + '|' + r.date, a.releaseIssue(r)])),
  list: list.map((r) => [r.name, r.lab, r.date, r.confirmed]),
}}));""")
    issues = out["issues"]
    assert issues["Opus 6|2026-10-14"] == "rumour or plan"
    assert issues["Decisions API|2026-10-22"] == "not a model"
    assert issues["Clairic|2026-10-19"] is None, "an 'other' row whose headline calls it a model counts"
    assert issues["Claude Opus 5.5|2026-10-29"] == "story is not the launch"
    assert issues["Kumo Relational|2026-10-21"] == "story is about something else"
    assert issues["Mischief|2026-10-30"] == "no named maker"
    assert issues["Deepseek V4.1 Flash (revision 1017)|2026-10-23"] is None
    names = {n: (lab, date, ok) for n, lab, date, ok in out["list"]}
    # Haiku's row with the launch words is 24 Oct; it was first reported, under another headline, on 7 Oct.
    assert names["Claude Haiku 5.5"] == ("Anthropic", "2026-10-07", True), names
    assert names["GPT-6.1 Terra"][2] is False, "one source and no story in the build: not confirmed"
    assert "Opus 6" not in names and "Decisions API" not in names and "Mischief" not in names


def test_newest_by_lab_needs_a_confirmed_launch_and_a_track_record():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    rows = [
        row("GPT-6", "OpenAI", "2026-10-01", "OpenAI releases GPT-6"),
        row("GPT-6.1 Terra", "OpenAI", "2026-10-26", "OpenAI launches GPT-6.1 Terra", sources=1),
        row("Gemini 4", "Google", "2026-10-02", "Google launches Gemini 4"),
        row("Gemini 4 Neon", "Google", "2026-10-20", "Google releases Gemini 4 Neon"),
        row("Solo 1", "One-off Labs", "2026-10-25", "One-off Labs releases Solo 1"),
    ]
    out = _run(f"""
const rows = a.withCanonicalLabs({json.dumps(rows)});
const list = a.releaseList(rows);
console.log(JSON.stringify(a.newestByLab(list, rows).map((r) => [r.lab, r.name, r.tracked])));""")
    assert out == [["Google", "Gemini 4 Neon", 2], ["OpenAI", "GPT-6", 2]], out


def test_month_pages_are_thin_below_the_minimum():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    rows = [row(f"Model {i}", "Lab A", f"2026-10-{10 + i:02d}", f"Lab A releases Model {i}") for i in range(5)]
    rows += [row("Model 9", "Lab A", "2026-09-20", "Lab A releases Model 9")]
    out = _run(f"""
const p = a.answerPages({json.dumps(rows)});
console.log(JSON.stringify({{ thin: [...p.thin], mod: Object.fromEntries(p.lastmod) }}));""")
    assert out["thin"] == ["/models/latest", "/models/released/2026-09"], out
    assert out["mod"]["/models/released/2026-10"] == "2026-10-14" and "/models/released/2026-09" in out["mod"], out


def _allowed(text: str, agent: str, path: str) -> bool:
    """robots.txt as Google and RFC 9309 read it: the group naming the agent (else *), and among its
    rules the longest matching path wins, Allow on a tie. Python's robotparser takes the first match
    instead, which would let "Allow: /" hide every Disallow under it."""
    groups = re.findall(r"((?:^User-agent:[^\n]*\n)+)((?:(?!User-agent:)[^\n]*\n?)*)", text, re.M)
    def agents(head):
        return [a.strip().lower() for a in re.findall(r"User-agent:\s*(\S+)", head)]
    rules = next((body for head, body in groups if agent.lower() in agents(head)), None)
    if rules is None:
        rules = next(body for head, body in groups if "*" in agents(head))
    best = (-1, True)
    for kind, rule in re.findall(r"^(Allow|Disallow):\s*(\S*)", rules, re.M):
        if rule and path.startswith(rule) and (len(rule) > best[0] or (len(rule) == best[0] and kind == "Allow")):
            best = (len(rule), kind == "Allow")
    return best[1]


def test_robots_welcomes_ai_crawlers_and_keeps_publisher_text_closed():
    text = (SITE / "public" / "robots.txt").read_text(encoding="utf-8")
    for agent in ["*", "Googlebot", "bingbot", *AI_AGENTS]:
        for path in ["/", "/today", "/models/latest", "/story/x", "/story/x.md", "/api/models.json", "/llms.txt", "/llms-full.txt"]:
            assert _allowed(text, agent, path), (agent, path)
        assert not _allowed(text, agent, "/ft/x.json"), agent
        for path in ["/search", "/admin"]:
            assert not _allowed(text, agent, path), (agent, path)
    for agent in AI_AGENTS:
        assert re.search(rf"^User-agent: {re.escape(agent)}$", text, re.M), agent


def test_llms_txt_points_to_the_answer_pages():
    text = (SITE / "public" / "llms.txt").read_text(encoding="utf-8")
    for path in ["/today", "/models/latest", "/models/released/", "/api/models.json", "/llms-full.txt", "/about"]:
        assert path in text, path
    assert "full text of the lead article" not in text, "story HTML for crawlers does not carry publishers' text"
    assert "—" not in text


def test_built_answer_files():
    dist = SITE / "dist"
    if not (dist / "models" / "latest.html").exists():
        print("SKIP site not built")
        return
    doc = json.loads((dist / "api" / "models.json").read_text(encoding="utf-8"))
    assert {"generatedAt", "license", "asOf", "newestByLab", "months", "releases"} <= doc.keys()
    assert not (set(_keys(doc)) & FORBIDDEN)
    for r in doc["newestByLab"]:
        assert r["confirmed"] is True and r["lab"] and r["firstReportedAt"], r
        assert r["lab"] not in ("Claude", "TBC"), r
    for name in ["today.md", "models/latest.md", "llms-full.txt"]:
        body = (dist / name).read_text(encoding="utf-8")
        assert re.search(r"\b\d{1,2} [A-Z][a-z]+ 20\d\d\b", body), f"{name} states no date"
        # Our own wording: headlines and digests quoted from stories keep their punctuation.
        own = "\n".join(line for line in body.splitlines() if "/story/" not in line and not line.startswith(("## ", "- ")))
        assert "—" not in own, f"{name} has an em dash in its own text"
        assert len(body.encode("utf-8")) < 60_000, name
    latest = (dist / "models" / "latest.html").read_text(encoding="utf-8")
    assert "As of " in latest and 'type="text/markdown"' in latest and '"FAQPage"' in latest
    sitemap = "".join(p.read_text(encoding="utf-8") for p in dist.glob("sitemap-*.xml"))
    for page in sorted((dist / "models" / "released").glob("*.html")):
        html = page.read_text(encoding="utf-8")
        listed = f"/models/released/{page.stem}<" in sitemap
        noindex = 'content="noindex' in html
        assert listed != noindex, f"{page.name}: sitemap and robots meta disagree"
    today = (dist / "today.html").read_text(encoding="utf-8")
    assert "The main AI news on" in today and "today.md" in today
    about = (dist / "about.html").read_text(encoding="utf-8")
    assert '"FAQPage"' in about and "not connected to digest.ai" in about


def _keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


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
