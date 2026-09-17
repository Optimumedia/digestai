"""Old addresses of merged stories on the site: python tests/test_site_redirects.py

The redirect list the site builds from (site/src/lib/redirects.mjs, run in Node), and, when the site
has been built with a redirects.json, the pages and sitemaps in site/dist."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"


def test_redirect_list_and_sitemap_filter():
    node = shutil.which("node")
    if not node:
        print("SKIP node not installed")
        return
    tmp = Path(tempfile.mkdtemp())
    (tmp / "stories.json").write_text(json.dumps([{"slug": "mistral-raises-3b"}, {"slug": "live-story"}]), encoding="utf-8")
    (tmp / "redirects.json").write_text(json.dumps([
        {"from": "mistral-raises-3b-dup", "to": "mistral-raises-3b"},
        {"from": "live-story", "to": "mistral-raises-3b"},    # a live page always wins
        {"from": "gone-story", "to": "unpublished-story"},    # nowhere to send readers
        {"from": "self", "to": "self"}]), encoding="utf-8")
    lib = (SITE / "src" / "lib" / "redirects.mjs").as_uri()
    probe = (f"import {{ loadRedirects, isRedirectPage }} from {json.dumps(lib)};\n"
             f"const r = loadRedirects({json.dumps(str(tmp))});\n"
             "console.log(JSON.stringify({r, pages: ['https://digestai.news/story/mistral-raises-3b-dup', 'https://digestai.news/story/mistral-raises-3b-dup/',"
             " 'https://digestai.news/story/mistral-raises-3b', 'https://digestai.news/topic/mistral-raises-3b-dup'].map((u) => isRedirectPage(u, r))}));")
    res = subprocess.run([node, "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    out = json.loads(res.stdout)
    assert out["r"] == [{"from": "mistral-raises-3b-dup", "to": "mistral-raises-3b"}], out
    assert out["pages"] == [True, True, False, False], out
    # The story route builds those pages, and the sitemap integration filters them out.
    route = (SITE / "src" / "pages" / "story" / "[slug].astro").read_text(encoding="utf-8")
    assert "storyRedirects.map" in route and "<StoryRedirect" in route
    assert "isRedirectPage(page, redirects)" in (SITE / "astro.config.mjs").read_text(encoding="utf-8")


def test_built_site_has_redirect_pages_outside_the_sitemaps():
    dist, data = SITE / "dist", SITE / "src" / "data" / "redirects.json"
    if not (dist / "sitemap-0.xml").exists() or not data.exists():
        print("SKIP site not built")
        return
    redirects = json.loads(data.read_text(encoding="utf-8"))
    live = {s["slug"] for s in json.loads((SITE / "src" / "data" / "stories.json").read_text(encoding="utf-8"))}
    sitemaps = (dist / "sitemap-0.xml").read_text(encoding="utf-8") + (dist / "news-sitemap.xml").read_text(encoding="utf-8")
    for r in redirects:
        if r["from"] in live or r["to"] not in live:
            continue
        page = (dist / "story" / f"{r['from']}.html").read_text(encoding="utf-8")
        target = f"/story/{r['to']}"
        assert f'http-equiv="refresh" content="0; url={target}"' in page
        assert re.search(rf'rel="canonical" href="https?://[^"]+{re.escape(target)}"', page)
        assert "location.replace" in page and "noindex" in page
        assert f"/story/{r['from']}<" not in sitemaps


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
