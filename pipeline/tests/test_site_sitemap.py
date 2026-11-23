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
    {"slug": "long-thread", "storyCount": 5, "updatedAt": "2026-09-21T00:00:00Z"},
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
        f" noindex: [...plan.noindex], quiet: [...plan.quiet], confirmed: {json.dumps([s['slug'] for s in STORIES])}.map((slug, i) => lib.storyConfirmed({json.dumps(STORIES)}[i]))}}));")


def test_sitemap_lists_confirmed_or_important_stories_and_hubs():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    stories = [f"/story/{s['slug']}" for s in STORIES] + ["/story/old-confirmed", "/story/old-single"]
    hubs = ["", "/today", "/week", "/category/policy", "/category/marketing", "/work", "/models", "/funding", "/api", "/about",
            "/listen", "/thread/long-thread", "/thread/big-thread", "/thread/small-thread", "/topic/anthropic", "/topic/small-co"]
    others = ["/daily/2026-09-20", "/week/2026-W38", "/threads", "/contact", "/privacy", "/terms", "/sources", "/subscribe"]
    out = _plan(stories + hubs + others)
    listed = set(out["listed"])
    # Stories offered to search: two or more publishers (by registrable domain), pinned, or importance 8+.
    assert out["confirmed"] == [True, False, True, True, False, False, False, True]
    for slug in ("two-publishers", "pinned-single", "future-dated"):
        assert f"/story/{slug}" in listed, slug
    # One publisher, even under two host names, even the company's own announcement, even at
    # importance 6: search-quiet (noindex for Google and Bing only), unlisted, still indexable for others.
    quiet = set(out["quiet"])
    for slug in ("same-publisher-twice", "primary-source", "important-single", "middling-single"):
        assert f"/story/{slug}" not in listed and f"/story/{slug}" in quiet and f"/story/{slug}" not in out["noindex"], slug
    # The thin one keeps its noindex for every crawler, as before, and is not repeated as quiet.
    assert "/story/thin-single" in out["noindex"] and "/story/thin-single" not in quiet
    assert "/story/thin-single" not in listed
    # Archive pages: listed only when two publishers covered the story.
    assert "/story/old-confirmed" in listed and "/story/old-single" not in listed
    # Hubs: the fixed ones, every category, threads of five or more.
    for p in ("", "/today", "/week", "/category/policy", "/category/marketing", "/models", "/funding", "/api",
              "/about", "/listen", "/threads", "/thread/long-thread"):
        assert p in listed, p
    # A three-story thread or topic hub is a list of headlines: quiet and unlisted. Under three stories
    # a topic is noindex for everyone (not repeated as quiet); a short thread is quiet.
    assert {"/thread/big-thread", "/thread/small-thread", "/topic/anthropic"} <= quiet
    assert not listed & {"/thread/big-thread", "/thread/small-thread", "/topic/anthropic", "/topic/small-co"}
    assert "/topic/small-co" in out["noindex"] and "/topic/small-co" not in quiet
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
    # /week leads with the current week's stories: it changes when the newest story does.
    assert mod["/week"] == mod[""]


def test_older_list_pages_stay_out_of_the_sitemap():
    """Page 2 and up of a long list (/topic/openai/2 …) is noindex and never listed; page 1 is."""
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    older = ["/topic/anthropic/2", "/models/gpt-6-astra/3", "/category/policy/2", "/daily/2026-09-20/2", "/threads/2",
             "/topic/anthropic/2/", "/category/policy/12"]
    firsts = ["/topic/anthropic", "/category/policy", "/threads", "/week", "/week/2026-W38"]
    out = _plan(older + firsts)
    listed = set(out["listed"])
    assert not listed & set(older), listed & set(older)
    for p in ("/category/policy", "/threads", "/week"):
        assert p in listed, p
    probe = _run(
        "console.log(JSON.stringify({size: lib.LIST_PAGE_SIZE, counts: [0, 1, 60, 61, 845].map((n) => lib.listPageCount(n)),"
        " older: ['/topic/x/2', '/threads/3', '/daily/2026-09-20/2.html', '/topic/x', '/week/2026-W38', '/work/week/2026-W38',"
        " '/threads', '/story/x', '/models/x/y'].map(lib.isOlderListPage)}));")
    assert probe["size"] == 60
    assert probe["counts"] == [1, 1, 1, 2, 15]
    assert probe["older"] == [True, True, True, False, False, False, False, False, False]


def test_list_templates_cap_their_rows():
    """Every long hub renders one page of its list and a pager; the older pages are marked paged."""
    pages = SITE / "src" / "pages"
    for hub, older in (("topic/[slug].astro", "topic/[slug]/[page].astro"),
                       ("models/[slug].astro", "models/[slug]/[page].astro"),
                       ("category/[category].astro", "category/[category]/[page].astro"),
                       ("daily/[date].astro", "daily/[date]/[page].astro"),
                       ("threads.astro", "threads/[page].astro")):
        src = (pages / hub).read_text(encoding="utf-8")
        assert "<Pager" in src, hub
        assert "list.map((s) => <StoryRow" not in src, f"{hub} renders its whole list"
        assert (pages / older).exists(), older
    older = (SITE / "src" / "components" / "OlderStories.astro").read_text(encoding="utf-8")
    threads = (pages / "threads" / "[page].astro").read_text(encoding="utf-8")
    assert "paged={true}" in older and "paged={true}" in threads
    base = (SITE / "src" / "layouts" / "Base.astro").read_text(encoding="utf-8")
    # A paged page is "noindex, follow" (thin) and left out of the site search.
    assert "const thin = thinProp || paged" in base and 'paged ? {} : { "data-pagefind-body": "" }' in base
    config = (SITE / "astro.config.mjs").read_text(encoding="utf-8")
    assert "isOlderListPage(p)" in config


def test_week_hub_answers_this_week():
    src = (SITE / "src" / "pages" / "week" / "index.astro").read_text(encoding="utf-8")
    assert "AI news this week" in src and '"ItemList"' in src and "list.slice(0, 10)" in src
    assert "/week/${key}" in src and "articleCount" in src


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


def test_search_quiet_rules():
    """Topic hubs need depth, the sitemap lists at most SITEMAP_MAX_STORIES stories (strongest first),
    the daily archive is never listed, and a one-story model page is quiet."""
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    out = _run(
        "const intro = {description: 'Anthropic is an AI safety company that makes the Claude models.'};"
        "const placeholder = {description: '7 stories since 1 October 2026, most recently X'};"
        "const topics = [[11, null], [12, null], [5, intro], [6, intro], [6, placeholder]].map(([n, i]) => lib.topicSearchWorthy(n, i));"
        "const threads = [4, 5].map((n) => lib.threadSearchWorthy({storyCount: n}));"
        "const daily = ['/daily', '/daily/2026-10-20', '/daily/2026-10-20/2', '/category/policy'].map(lib.isDailyArchive);"
        "const art = (d, t) => ({domain: d, url: 'https://' + d + '/a', publishedAt: t});"
        "const many = [];"
        "for (let i = 0; i < lib.SITEMAP_MAX_STORIES + 20; i++) {"
        "  const t = new Date(Date.UTC(2026, 9, 1) + i * 6e5).toISOString();"
        "  many.push({id: i, slug: 's' + i, importance: 5, articleCount: 2, articles: [art('a.com', t), art('b.com', t)], firstPublishedAt: t, updatedAt: t});"
        "}"
        "const old = '2026-01-01T00:00:00Z';"
        "many.push({id: 9001, slug: 'old-wide', importance: 5, articleCount: 3, articles: [art('a.com', old), art('b.com', old), art('c.com', old)], firstPublishedAt: old, updatedAt: old});"
        "const plan = lib.sitemapIndex({stories: many});"
        "const listed = many.filter((s) => plan.include('/story/' + s.slug)).map((s) => s.slug);"
        "const models = [{name: 'Solo 1', lab: 'X', license: 'MIT', storySlug: 's1', date: '2026-10-01'}, {name: 'Duo 2', lab: 'X', storySlug: 's2', date: '2026-10-01'}];"
        "const ents = [{name: 'Duo 2', kind: 'models', storyIds: [2, 3]}];"
        "const q = lib.quietPaths({stories: many.slice(0, 10), entities: ents, models});"
        "console.log(JSON.stringify({topics, threads, daily, n: listed.length, max: lib.SITEMAP_MAX_STORIES,"
        " oldWide: listed.includes('old-wide'), newest: listed.includes('s' + (lib.SITEMAP_MAX_STORIES + 19)), oldest: listed.includes('s0'),"
        " dailyListed: plan.include('/daily/2026-10-20'), soloQuiet: q.has('/models/solo-1'), duoQuiet: q.has('/models/duo-2')}));")
    assert out["topics"] == [False, True, False, True, False], out["topics"]
    assert out["threads"] == [False, True]
    assert out["daily"] == [True, True, False, False]
    assert out["n"] == out["max"]
    # Three publishers beat two however old; among equals the newest win.
    assert out["oldWide"] and out["newest"] and not out["oldest"]
    assert not out["dailyListed"]
    assert out["soloQuiet"] and not out["duoQuiet"]


def test_indexnow_submits_what_the_site_offers():
    """pipeline/digest/indexnow.py applies the same rule as indexing.mjs storySearchWorthy."""
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    from digest import indexnow

    extra = [_story(20, "hot-single", 8, [_art("example.com")]), _story(21, "no-articles", 9, [])]
    stories = STORIES + extra
    js = _run(f"console.log(JSON.stringify({json.dumps(stories)}.map(lib.storySearchWorthy)));")
    assert js == [indexnow.search_worthy(s) for s in stories], js
    assert js == [True, False, False, True, False, False, False, True, True, True], js

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stories.json"
        path.write_text(json.dumps(stories), encoding="utf-8")
        wanted = indexnow.story_filter(path)
        # Offered stories go; quiet ones do not; a slug no longer exported (merged, now a redirect) goes.
        assert wanted("two-publishers") and wanted("hot-single") and wanted("merged-away")
        assert not wanted("middling-single") and not wanted("primary-source")
        assert indexnow.story_filter(Path(tmp) / "missing.json")("anything")


def test_pages_carry_the_quiet_tags_and_no_full_text_stub():
    base = (SITE / "src" / "layouts" / "Base.astro").read_text(encoding="utf-8")
    assert '<meta name="googlebot" content="noindex, follow" />' in base and '<meta name="bingbot" content="noindex, follow" />' in base
    assert "— Digest AI" not in base and "| Digest AI" in base
    story = (SITE / "src" / "pages" / "story" / "[slug].astro").read_text(encoding="utf-8")
    assert "quiet={!storySearchWorthy(story)}" in story
    # The HTML holds the empty box only; the heading, loading line and attribution come from app.js.
    for text in ("Loading the full article", "reproduced here", "needs JavaScript", "{full.title}"):
        assert text not in story, text
    app = (SITE / "public" / "app.js").read_text(encoding="utf-8")
    assert "Loading the full article" in app and "reproduced here with attribution" in app
    assert "did not load" not in app
    for page, mark in (("topic/[slug].astro", "quiet={isQuiet(base)}"), ("models/[slug].astro", "quiet={isQuiet(base)}"),
                       ("thread/[slug].astro", "quiet={isQuiet("), ("daily/[date].astro", "quiet={true}"),
                       ("story/[...archived].astro", '<meta name="googlebot" content="noindex, follow" />')):
        assert mark in (SITE / "src" / "pages" / page).read_text(encoding="utf-8"), page
    assert "storySearchWorthy(s)" in (SITE / "src" / "pages" / "news-sitemap.xml.ts").read_text(encoding="utf-8")


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
        assert ft["html"] and ft["title"] and ft["url"] and f'data-fulltext="/ft/{s["slug"]}.json"' in html
        assert "Loading the full article" not in html and "reproduced here" not in html, s["slug"]
        # A page offered to search is in no robots tag's noindex; a quiet one is noindex for Google
        # and Bing only, keeps its canonical, and is not in the sitemap.
        quiet = '<meta name="googlebot" content="noindex, follow"' in html
        assert quiet == ('<meta name="bingbot" content="noindex, follow"' in html), s["slug"]
        if quiet:
            assert f"/story/{s['slug']}" not in locs and 'rel="canonical"' in html, s["slug"]
        checked += 1
    assert checked, "no story with full text in the build"
    assert not any(p.startswith("/ft/") for p in locs)
    assert "Disallow: /ft/" in (dist / "robots.txt").read_text(encoding="utf-8")
    # Older pages of long lists: built, "noindex, follow", out of the sitemap and the site search.
    older = [p for p in dist.glob("*/**/*.html") if re.search(r"[\\/](?:topic|models|category|daily)[\\/][^\\/]+[\\/]\d+\.html$|[\\/]threads[\\/]\d+\.html$", str(p))]
    for page in older:
        rel = "/" + page.relative_to(dist).as_posix()[:-5]
        assert rel not in locs, rel
        html = page.read_text(encoding="utf-8")
        assert 'content="noindex, follow"' in html and 'rel="canonical"' not in html and "data-pagefind-body" not in html, rel
    # No hub page carries a whole long list any more.
    for hub in [*(dist / "topic").glob("*.html"), *(dist / "models").glob("*.html"), *(dist / "category").glob("*.html"),
                *(dist / "daily").glob("*.html"), dist / "threads.html", dist / "week.html"]:
        if hub.exists():
            assert hub.stat().st_size < 250_000, f"{hub.relative_to(dist)} is {hub.stat().st_size} bytes"


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
