"""Machine-readable story files for AI assistants: python tests/test_site_api.py

The builder (site/src/lib/api.ts) never copies publishers' text, and, when the site has been built,
every /story/<slug>.json in site/dist parses, has the fields /api documents and no full-text field;
the index files parse and the JSON stays out of the sitemaps."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"
REQUIRED = {"url", "headline", "summary", "keyPoints", "category", "firstPublishedAt", "updatedAt", "sources", "cite", "license"}
LIVE_ONLY = {"whyItMatters", "entities", "sourceNotes", "thread"}
SOURCE_FIELDS = {"outlet", "title", "url"}
# Publisher text in the export (Article.contentMd, Article.description) and anything named like it.
FORBIDDEN = {"contentMd", "content", "fullText", "full_text", "text_md", "body", "description", "articleBody"}


def _keys(value, out: set[str]) -> set[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


def test_builder_never_reads_publisher_text():
    src = (SITE / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)  # comments may name the fields
    assert ".contentMd" not in code and ".description" not in code, "api.ts must not copy article text"
    # Story pages point to the two copies.
    page = (SITE / "src" / "pages" / "story" / "[slug].astro").read_text(encoding="utf-8")
    assert 'type="application/json"' in page and 'type="text/markdown"' in page
    assert "/api" in (SITE / "public" / "llms.txt").read_text(encoding="utf-8")


def test_built_story_json():
    dist = SITE / "dist"
    files = sorted((dist / "story").glob("*.json")) if (dist / "story").exists() else []
    if not files:
        print("SKIP site not built")
        return
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        missing = REQUIRED - doc.keys()
        if not doc.get("archived"):
            missing |= LIVE_ONLY - doc.keys()
        assert not missing, f"{f.name} lacks {sorted(missing)}"
        bad = _keys(doc, set()) & FORBIDDEN
        assert not bad, f"{f.name} carries {sorted(bad)}"
        slug = f.stem
        assert doc["url"].endswith(f"/story/{slug}") and doc["slug"] == slug
        assert doc["cite"]["text"].startswith("Digest AI, ") and doc["cite"]["text"].endswith(doc["url"])
        assert doc["headline"] and isinstance(doc["keyPoints"], list)
        for s in doc["sources"]:
            assert SOURCE_FIELDS <= s.keys(), (f.name, s)
            assert s["url"].startswith("http")
        if not doc.get("archived"):
            assert all({"publishedAt", "primary"} <= s.keys() for s in doc["sources"])
            assert doc["updatedAt"] is None or doc["firstPublishedAt"] is None or doc["updatedAt"] >= doc["firstPublishedAt"]
        md = f.with_suffix(".md")
        assert md.exists(), f"{md.name} missing"
        assert doc["cite"]["text"] in md.read_text(encoding="utf-8")
        # Never larger than it needs to be: our text and links, not an article.
        assert f.stat().st_size < 40_000, f"{f.name} is {f.stat().st_size} bytes"


def test_built_index_files():
    dist = SITE / "dist"
    if not (dist / "api" / "latest.json").exists():
        print("SKIP site not built")
        return
    for f in [dist / "api" / "latest.json", dist / "api" / "briefing.json", *sorted((dist / "api" / "category").glob("*.json"))]:
        doc = json.loads(f.read_text(encoding="utf-8"))
        assert {"generatedAt", "license", "docs", "stories"} <= doc.keys(), f.name
        assert "/terms" in doc["license"]
        assert len(doc["stories"]) <= 100
        for row in doc["stories"]:
            assert row["json"].endswith(f"/story/{row['slug']}.json")
            assert (dist / "story" / f"{row['slug']}.json").exists(), row["slug"]
        assert f.stat().st_size < 150_000, f"{f.name} is {f.stat().st_size} bytes"
    assert (dist / "api.html").exists()
    sitemaps = "".join(p.read_text(encoding="utf-8") for p in dist.glob("*sitemap*.xml"))
    assert not re.search(r"<loc>[^<]*\.(json|md)</loc>", sitemaps)


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
