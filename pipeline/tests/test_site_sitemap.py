"""Which pages the sitemap lists: python tests/test_site_sitemap.py

The sitemap plan (site/src/lib/indexing.mjs sitemapIndex, run in Node): confirmed or important
stories and the hub pages, never a noindex page; single-source middling stories stay indexable but
unlisted; lastmod is the date the story page shows. When the site has been built, also that the
built sitemap follows the plan, that story pages carry none of the publisher's text and that
robots.txt blocks the full-text files."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SITE = Path(__file__).resolve().parents[2] / "site"


def _run(script: str) -> dict:
    lib = (SITE / "src" / "lib" / "indexing.mjs").as_uri()
    probe = f"import * as lib from {json.dumps(lib)};\n{script}"
    res = subprocess.run([shutil.which("node"), "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    return json.loads(res.stdout)


def _art(domain: str, published: str = "2026-09-20T08:00:00Z") -> dict:
    return {"domain": domain, "url": f"https://{domain}/a", "publishedAt": published}


def _story(i: int, slug: str, importance: int, arts: list[dict], **extra) -> dict:
    base = {"id": i, "slug": slug, "importance": importance, "articles": arts, "articleCount": len(arts),
            "hasPrimary": False, "pinned": False, "category": "policy",
            "firstPublishedAt": "2026-09-20T08:00:00Z", "updatedAt": "2026-09-21T09:00:00Z"}
    base.update(extra)
    return base


STORIES = [
    _story(1, "two-publishers", 5, [_art("theverge.com"), _art("news.bbc.co.uk", "2026-09-20T12:30:00Z")], articleCount=2),
    _story(2, "same-publisher-twice", 5, [_art("techcrunch.com"), _art("www.techcrunch.com")], articleCount=2),
    _story(3, "primary-source", 5, [_art("openai.com")], hasPrimary=True),
    _story(4, "pinned-single", 5, [_art("example.com")], pinned=True),
    _story(5, "important-single", 6, [_art("example.com")]),
    _story(6, "middling-single", 5, [_art("example.com")]),
    _story(7, "thin-single", 4, [_art("example.com")]),
    # A feed's future-dated article cannot move the date past updatedAt.
    _story(8, "future-dated", 7, [_art("a.com"), _art("b.com", "2027-01-01T00:00:00Z")], articleCount=2,
           updatedAt="2026-09-20T10:00:00Z"),
]
ENTITIES = [
    {"name": "Anthropic", "kind": "companies", "storyIds": [1, 3, 5]},
    {"name": "Small Co", "kind": "companies", "storyIds": [6]},
]
THREADS = [
    {"slug": "big-thread", "storyCount": 3, "updatedAt": "2026-09-21T00:00:00Z"},
    {"slug": "small-thread", "storyCount": 2, "updatedAt": "2026-09-21T00:00:00Z"},
]
ARCHIVED = [
    {"slug": "old-confirmed", "updatedAt": "2026-07-01T00:00:00Z",
     "sources": [{"url": "https://www.reuters.com/x"}, {"url": "https://apnews.com/y"}]},
    {"slug": "old-single", "updatedAt": "2026-07-01T00:00:00Z",
     "sources": [{"url": "https://reuters.com/x"}, {"url": "https://www.reuters.com/z"}]},
]


def _plan(paths: list[str]) -> dict:
    data = {"stories": STORIES, "entities": ENTITIES, "models": [], "threads": THREADS, "archived": ARCHIVED}
    return _run(
        f"const plan = lib.sitemapIndex({json.dumps(data)});\n"
        f"const paths = {json.dumps(paths)};\n"
        "console.log(JSON.stringify({listed: paths.filter((p) => plan.include(p)), lastmod: Object.fromEntries(plan.lastmod),"
        f" noindex: [...plan.noindex], confirmed: {json.dumps([s['slug'] for s in STORIES])}.map((slug, i) => lib.storyConfirmed({json.dumps(STORIES)}[i]))}}));")


def test_sitemap_lists_confirmed_or_important_stories_and_hubs():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    stories = [f"/story/{s['slug']}" for s in STORIES] + ["/story/old-confirmed", "/story/old-single"]
    hubs = ["", "/today", "/category/policy", "/category/marketing", "/work", "/models", "/funding", "/api", "/about",
            "/listen", "/thread/big-thread", "/thread/small-thread", "/topic/anthropic", "/topic/small-co"]
    others = ["/daily/2026-09-20", "/week/2026-W38", "/threads", "/contact", "/privacy", "/terms", "/sources", "/subscribe"]
    out = _plan(stories + hubs + others)
    listed = set(out["listed"])
    # Stories: confirmed (two publishers by registrable domain, a primary source, pinned) or importance 6+.
    assert out["confirmed"] == [True, False, True, True, False, False, False, True]
    for slug in ("two-publishers", "primary-source", "pinned-single", "important-single", "future-dated"):
        assert f"/story/{slug}" in listed, slug
    # One publisher under two host names is one source; a middling single-source story is unlisted...
    assert "/story/same-publisher-twice" not in listed and "/story/middling-single" not in listed
    # ...but not noindex: it stays indexable and linked. The thin one keeps its noindex, as before.
    assert "/story/middling-single" not in out["noindex"] and "/story/thin-single" in out["noindex"]
    assert "/story/thin-single" not in listed
    # Archive pages: listed only when two publishers covered the story.
    assert "/story/old-confirmed" in listed and "/story/old-single" not in listed
    # Hubs: the fixed ones, every category, threads of three or more, topics above the threshold.
    for p in ("", "/today", "/category/policy", "/category/marketing", "/models", "/funding", "/api",
              "/about", "/listen", "/threads", "/thread/big-thread", "/topic/anthropic"):
        assert p in listed, p
    assert "/thread/small-thread" not in listed and "/topic/small-co" not in listed
    # Everything else stays out of the sitemap (still built, linked and indexable).
    # Weekly recaps with enough stories are listed too (they carry their own intro).
    assert not listed & set(others) - {p for p in others if p.startswith("/week/") or p == "/threads"}, listed & set(others)
    # /work is noindex with fewer than three practical cards, so it is not listed here.
    assert "/work" in out["noindex"] and "/work" not in listed


def test_sitemap_lastmod_is_the_date_the_page_shows():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    mod = _plan([])["lastmod"]
    # The newest source the page lists, not updatedAt (which moves when an article is only counted).
    assert mod["/story/two-publishers"] == "2026-09-20T12:30:00Z"
    assert mod["/story/primary-source"] == "2026-09-20T08:00:00Z"
    assert mod["/story/future-dated"] == "2026-09-20T10:00:00Z"
    # Hubs take their newest story's date; archive pages their own.
    assert mod["/category/policy"] == "2026-09-20T12:30:00Z" and mod[""] == "2026-09-20T12:30:00Z"
    assert mod["/topic/anthropic"] == "2026-09-20T12:30:00Z"
    assert mod["/story/old-confirmed"] == "2026-07-01T00:00:00Z"


def test_registrable_matches_the_pipeline():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    from digest.hold import registrable

    hosts = ["news.bbc.co.uk", "www.theverge.com", "blog.openai.com", "example.com.au", "a.b.gov.uk", "localhost",
             "WWW.Reuters.com.", "user@mail.example.org:443", "sub.example.ne.jp", ""]
    out = _run(f"console.log(JSON.stringify({json.dumps(hosts)}.map(lib.registrable)));")
    assert out == [registrable(h) for h in hosts], out


def test_templates_and_robots_keep_publishers_text_out_of_the_html():
    story = (SITE / "src" / "pages" / "story" / "[slug].astro").read_text(encoding="utf-8")
    assert 'data-read="full_text"' in story and "data-fulltext=" in story
    assert "renderMarkdown(full.contentMd)" not in story and "contentMd" not in story
    assert "/ft/" in (SITE / "public" / "app.js").read_text(encoding="utf-8")
    robots = (SITE / "public" / "robots.txt").read_text(encoding="utf-8")
    # Every group blocks /ft/ (a crawler with its own group ignores the * group) and nothing new besides.
    groups = [g for g in re.split(r"\n(?=User-agent:)", robots) if g.startswith("User-agent:")]
    assert groups and all("Disallow: /ft/" in g for g in groups), robots
    assert sorted(re.findall(r"Disallow: (\S+)", robots)) == ["/admin", "/ft/", "/ft/", "/search"]


def test_built_site():
    dist = SITE / "dist"
    data = SITE / "src" / "data" / "stories.json"
    if not (dist / "sitemap-0.xml").exists() or not data.exists():
        print("SKIP site not built")
        return
    stories = [s for s in json.loads(data.read_text(encoding="utf-8")) if s.get("articles")]
    sitemap = (dist / "sitemap-0.xml").read_text(encoding="utf-8")
    locs = set(re.findall(r"<loc>https?://[^/<]+([^<]*)</loc>", sitemap))
    checked = 0
    for s in stories:
        full = next((a for a in s["articles"] if a["id"] == s.get("leadArticleId") and a.get("contentMd")), None) \
            or next((a for a in s["articles"] if a.get("contentMd")), None)
        page = dist / "story" / f"{s['slug']}.html"
        if not full or not page.exists():
            continue
        html = page.read_text(encoding="utf-8")
        # No sentence of the publisher's article is in the page; the /ft/ file has it.
        lines = [ln.strip() for ln in full["contentMd"].split("\n") if len(ln.strip()) > 80]
        for ln in lines[:5]:
            probe = re.sub(r"[*_`#>\[\]()]", "", ln)[20:70]
            assert probe not in html, (s["slug"], probe)
        ft = json.loads((dist / "ft" / f"{s['slug']}.json").read_text(encoding="utf-8"))
        assert ft["html"] and f'data-fulltext="/ft/{s["slug"]}.json"' in html
        checked += 1
    assert checked, "no story with full text in the build"
    assert not any(p.startswith("/ft/") for p in locs)
    assert "Disallow: /ft/" in (dist / "robots.txt").read_text(encoding="utf-8")


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
