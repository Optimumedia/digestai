"""The most important built pages work: python tests/test_site_smoke.py (after `npm run build` in site/)

CI builds the site from the test fixture (tests/site_fixture.py) and runs this before anything is
deployed; it also works on a build from a real export. For home, /today, a story, /models, /work,
/admin and /admin/team: the page is complete, shows no template leftovers ("undefined", "NaN",
"[object Object]"), does not show its empty state when the data it is built from has items, and links
to what that data says it should. Every internal link on every built page leads to a built file."""
from __future__ import annotations

import html as htmllib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DIST, SITE_DATA, main, skip  # noqa: E402

LEFTOVERS = [r"\bundefined\b", r"\bNaN\b", r"\[object Object\]", r"Invalid Date", r"\{\{", r"\bnull\b(?! and)"]
# Folders the pipeline fills at run time (share cards, audio, media) are not in a CI build.
RUNTIME_PREFIXES = ("/og/", "/audio/", "/media/", "/cdn-cgi/")


def _read(name: str, fallback):
    try:
        return json.loads((SITE_DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _built() -> bool:
    if not (DIST / "index.html").exists() or not (SITE_DATA / "stories.json").exists():
        skip("site not built")
        return False
    return True


def _page(rel: str) -> str:
    path = DIST / rel
    assert path.exists(), f"{rel} was not built"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 2000, f"{rel} is only {len(text)} bytes"
    # (Astro may put a page's own script after </html>; browsers run it, so that is allowed.)
    assert "<html" in text and "</body>" in text and "</html>" in text, f"{rel} is not a complete page"
    title = re.search(r"<title>([^<]*)</title>", text)
    assert title and title.group(1).strip(), f"{rel} has no title"
    return text


def visible_text(page: str) -> str:
    page = re.sub(r"<(script|style|template)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    page = re.sub(r"<[^>]+>", " ", page)
    return htmllib.unescape(re.sub(r"\s+", " ", page))


def attributes(page: str) -> str:
    """The values of attributes a reader sees or a crawler reads (alt, title, aria-label, content)."""
    return " ".join(htmllib.unescape(v) for v in re.findall(r'\s(?:alt|title|aria-label|content|placeholder)="([^"]*)"', page))


def _no_leftovers(rel: str, page: str) -> None:
    text = visible_text(page) + " " + attributes(page)
    for pat in LEFTOVERS:
        m = re.search(pat, text)
        assert not m, f"{rel} shows {m.group(0)!r}: ...{text[max(0, m.start() - 80):m.end() + 80]}..."


def _links(page: str) -> set[str]:
    return {htmllib.unescape(h) for h in re.findall(r'<a\b[^>]*?\shref="([^"]+)"', page)}


def _stories() -> dict[int, dict]:
    return {s["id"]: s for s in _read("stories.json", [])}


def _briefing_slugs() -> list[str]:
    stories = _stories()
    return [stories[i]["slug"] for i in _read("briefing.json", {}).get("storyIds", []) if i in stories]


def test_home_and_today_lead_with_the_briefing():
    if not _built():
        return
    slugs = _briefing_slugs()
    assert slugs, "the data has no briefing: the build would show an empty front page"
    for rel in ("index.html", "today.html"):
        page = _page(rel)
        _no_leftovers(rel, page)
        assert "No stories yet" not in page, f"{rel} shows its empty state"
        links = _links(page)
        missing = [s for s in slugs if f"/story/{s}" not in links]
        assert not missing, f"{rel} does not link to the briefing stories {missing}"


def test_a_story_page_shows_the_story_and_every_source():
    if not _built():
        return
    stories = _stories()
    assert stories, "the data has no stories"
    # The briefing's first story (or the best-covered one): the page lists every outlet.
    ids = [i for i in _read("briefing.json", {}).get("storyIds", []) if i in stories]
    lead = stories[ids[0]] if ids else max(stories.values(), key=lambda s: len(s.get("articles") or []))
    rel = f"story/{lead['slug']}.html"
    page = _page(rel)
    _no_leftovers(rel, page)
    text = visible_text(page)
    assert lead["headline"] in text, f"{rel} does not show its headline"
    for point in lead.get("keyPoints") or []:
        assert point in text, f"{rel} lacks the key point {point!r}"
    links = _links(page)
    for art in lead["articles"]:
        assert art["url"] in links, f"{rel} does not link to its source {art['url']}"
    assert 'rel="canonical"' in page and "application/ld+json" in page


def test_models_and_work_list_what_the_data_has():
    if not _built():
        return
    page = _page("models.html")
    _no_leftovers("models.html", page)
    models = _read("trackers.json", {}).get("models") or []
    if models:
        assert "No releases recorded yet" not in page, "/models shows its empty state with releases in the data"
        text = visible_text(page)
        assert any(m["name"] in text for m in models), "/models names none of the releases in the data"
    work_rel = "work.html" if (DIST / "work.html").exists() else "work/index.html"
    page = _page(work_rel)
    _no_leftovers(work_rel, page)
    work = _read("work.json", {})
    if work.get("storyIds"):
        assert "Nothing here yet" not in page, "/work shows its empty state with cards in the data"
        text = visible_text(page)
        tools = {t["tool"] for t in work.get("tools") or []}
        assert any(t in text for t in tools), f"/work names none of the tools {sorted(tools)}"


def test_admin_pages_show_the_pipeline_and_the_team():
    if not _built():
        return
    page = _page("admin.html")
    _no_leftovers("admin.html", page)
    if _read("admin.json", None):
        assert "No admin data yet" not in page, "/admin shows its empty state with admin.json present"
    assert 'name="robots" content="noindex' in page, "/admin must stay out of search"
    page = _page("admin/team.html")
    _no_leftovers("admin/team.html", page)
    team = _read("team.json", None)
    if team:
        assert "The team starts on the next pipeline run" not in page, "/admin/team shows its empty state with team.json present"
        text = visible_text(page)
        names = [r.get("name") or rid for rid, r in (team.get("roles") or {}).items()]
        assert len(names) == 6, names
        missing = [n for n in names if n not in text]
        assert not missing, f"/admin/team does not show the roles {missing}"


def resolve(href: str) -> Path | None:
    """The built file an internal link leads to, or None when there is none."""
    path = href.split("#", 1)[0].split("?", 1)[0]
    if not path or path == "/":
        return DIST / "index.html"
    rel = path.lstrip("/")
    for cand in (DIST / rel, DIST / f"{rel.rstrip('/')}.html", DIST / rel / "index.html"):
        if cand.is_file():
            return cand
    return None


def test_internal_links_lead_to_built_pages():
    if not _built():
        return
    broken: dict[str, list[str]] = {}
    pages = sorted(DIST.rglob("*.html"))
    assert len(pages) > 20, f"only {len(pages)} pages built"
    for page in pages:
        if "pagefind" in page.parts:
            continue
        rel = page.relative_to(DIST).as_posix()
        for href in _links(page.read_text(encoding="utf-8")):
            if not href.startswith("/") or href.startswith("//") or href.startswith(RUNTIME_PREFIXES):
                continue
            if resolve(href) is None:
                broken.setdefault(href.split("#", 1)[0], []).append(rel)
    lines = [f"{href} (from {', '.join(sorted(srcs)[:3])}{'...' if len(srcs) > 3 else ''})" for href, srcs in sorted(broken.items())]
    assert not broken, f"{len(broken)} internal links lead nowhere:\n  " + "\n  ".join(lines[:40])


def test_link_resolution_rules():
    # The rules themselves, so the check above cannot pass by resolving everything.
    assert resolve("/no-such-page-anywhere") is None
    assert resolve("/#top") == DIST / "index.html"


if __name__ == "__main__":
    main(globals())
