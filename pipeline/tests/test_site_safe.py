"""Other people's words in our pages: python tests/test_site_safe.py

Headlines, summaries and publishers' text come from feeds, web pages and free AI models. The site
writes them through site/src/lib/safe.mjs (run here in Node): JSON inside <script> elements cannot end
the element, and Markdown keeps only web, mail and on-site addresses in links and pictures. When the
site has been built, the pages in site/dist are checked too."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"

BAD_LINKS = [
    "[read more](javascript:alert(document.domain))",
    "[read more](JaVaScRiPt:alert(1))",
    "[read more](java\tscript:alert(1))",
    "[read more](javascript&#58;alert(1))",
    "[read more](&#106;avascript:alert(1))",
    "[read more](%6Aavascript:alert(1))",
    "[read more](vbscript:msgbox(1))",
    "[read more](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[read more][r]\n\n[r]: javascript:alert(1)",
]
GOOD_LINKS = {
    "[the paper](https://arxiv.org/abs/2401.00001)": 'href="https://arxiv.org/abs/2401.00001"',
    "[write to us](mailto:hello@digestai.news)": 'href="mailto:hello@digestai.news"',
    "[the story](/story/openai-ships-gpt-6)": 'href="/story/openai-ships-gpt-6"',
    "[up](#top)": 'href="#top"',
    "[query](/search?q=a&b=c)": 'href="/search?q=a&b=c"',
    "<https://example.com/a>": "&lt;",  # raw angle brackets are shown, as before
}


def _node(probe: str):
    node = shutil.which("node")
    if not node:
        print("SKIP node not installed")
        return None
    if not (SITE / "node_modules" / "marked").exists():
        print("SKIP site/node_modules not installed (npm ci in site/)")
        return None
    res = subprocess.run([node, "--input-type=module", "-e", probe], capture_output=True, text=True,
                         timeout=60, encoding="utf-8", cwd=SITE)  # so 'marked' resolves from site/node_modules
    assert res.returncode == 0, res.stderr[:800]
    return json.loads(res.stdout)


def test_json_in_a_script_cannot_end_it():
    lib = (SITE / "src" / "lib" / "safe.mjs").as_uri()
    value = {"headline": "How </script><script>alert(1)</script> works", "note": "a b & <!-- c -->"}
    out = _node(f"import {{ jsonForScript }} from {json.dumps(lib)};\n"
                f"const v = {json.dumps(value)};\n"
                "const s = jsonForScript(v);\n"
                "console.log(JSON.stringify({ s, back: JSON.parse(s), js: eval('(' + s + ')') }));")
    if out is None:
        return
    assert "<" not in out["s"] and ">" not in out["s"] and "&" not in out["s"], out["s"]
    assert " " not in out["s"]
    assert out["back"] == value and out["js"] == value


def test_markdown_keeps_only_safe_addresses():
    lib = (SITE / "src" / "lib" / "safe.mjs").as_uri()
    cases = BAD_LINKS + list(GOOD_LINKS) + [
        "![chart](https://example.com/chart.png)",
        "![chart](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)",
        "![chart](javascript:alert(1))",
        "<img src=x onerror=alert(1)> and <script>alert(1)</script>",
        "## A heading\n\nA paragraph with **bold**, a [link](https://example.com \"its title\") and `code`.\n\n- one\n- two",
    ]
    out = _node(f"import {{ markdownToHtml, safeUrl }} from {json.dumps(lib)};\n"
                "import { marked } from 'marked';\n"
                f"const cases = {json.dumps(cases)};\n"
                "const esc = (m) => m.replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
                "console.log(JSON.stringify({ html: cases.map((c) => markdownToHtml(c)),"
                " plain: cases.map((c) => marked.parse(esc(c), { async: false, gfm: true, breaks: false })),"
                " empty: [markdownToHtml(''), markdownToHtml(null)],"
                " urls: ['', ' ', 'https://a.b', '//a.b/x', 'page.html', 'a/b:c', 'foo&bar', 'mailto:x@y.z', 'tel:1'].map((u) => safeUrl(u)) }));")
    if out is None:
        return
    html = dict(zip(cases, out["html"]))
    plain = dict(zip(cases, out["plain"]))
    # Why this exists: marked on its own (the site's renderer until 26 Nov 2026) links any address.
    assert 'href="javascript:' in plain[BAD_LINKS[0]], plain[BAD_LINKS[0]]
    for case in BAD_LINKS:
        h = html[case].lower()
        assert "<a" not in h and "href" not in h, (case, h)
        assert "read more" in h, (case, h)  # the words stay, the address goes
    for case, want in GOOD_LINKS.items():
        assert want in html[case], (case, html[case])
        assert html[case] == plain[case], (case, html[case], plain[case])  # unchanged from before
    assert 'src="https://example.com/chart.png"' in html["![chart](https://example.com/chart.png)"]
    for bad in ("![chart](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)", "![chart](javascript:alert(1))"):
        assert "<img" not in html[bad] and "chart" in html[bad], html[bad]
    raw = html["<img src=x onerror=alert(1)> and <script>alert(1)</script>"]
    assert "<img" not in raw and "<script" not in raw and "&lt;img" in raw, raw
    ordinary = cases[-1]
    assert html[ordinary] == plain[ordinary], (html[ordinary], plain[ordinary])
    assert out["empty"] == ["", ""]
    assert out["urls"] == [False, False, True, True, True, True, False, True, False], out["urls"]


def test_built_pages_hold_no_escaping_script_or_script_address():
    dist = SITE / "dist"
    if not (dist / "index.html").exists():
        print("SKIP site not built")
        return
    ld = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
    pages = 0
    for page in dist.rglob("*.html"):
        # Our own offline page's back link is the one script address the templates write themselves.
        text = page.read_text(encoding="utf-8", errors="replace").replace('href="javascript:history.back()"', "")
        pages += 1
        for block in ld.findall(text):
            assert "<" not in block, (page, block[:200])
            json.loads(block)
        assert not re.search(r"""(href|src)\s*=\s*["']?\s*(javascript|vbscript|data:text)""", text, re.I), page
    for doc in (dist / "ft").glob("*.json"):
        html = json.loads(doc.read_text(encoding="utf-8")).get("html", "")
        assert not re.search(r"""(href|src)\s*=\s*["']?\s*(javascript|vbscript|data:)""", html, re.I), doc
    assert pages > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
