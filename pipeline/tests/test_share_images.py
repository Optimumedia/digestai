"""The company's own share image (images.primary_images and site/src/lib/data.ts primaryImage).
Offline, fake HTTP: python tests/test_share_images.py. When the site has been built, the built story
pages are checked too: a story listed in primary-images.json declares that picture as og:image and
twitter:image with its size; any other story declares our card."""
from __future__ import annotations

import html
import io
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import images  # noqa: E402

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
SITE = Path(__file__).resolve().parents[2] / "site"


def picture(w: int, h: int, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "#336699").save(buf, fmt)
    return buf.getvalue()


def story(slug: str, *arts: dict, first: str = "2026-09-21T08:00:00Z") -> dict:
    return {"slug": slug, "firstPublishedAt": first, "articles": list(arts)}


def art(domain: str, image: str, stype: str = "primary", lead: bool = False, url: str | None = None) -> dict:
    return {"domain": domain, "url": url or f"https://{domain}/news/post", "imageUrl": image,
            "sourceType": stype, "isLead": lead, "source": domain}


class Web:
    """Fake image server: url -> (status, content type, bytes). Counts the requests."""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def __call__(self, url):
        self.calls.append(url)
        status, ctype, body = self.pages.get(url, (404, "text/html", b""))
        return status, ctype, body[:images.PRIMARY_HEAD_BYTES], url


def run(stories, web, cache=None, now=NOW, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        cache_path, out_path = Path(tmp) / "cache.json", Path(tmp) / "out.json"
        if cache is not None:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        stats = images.primary_images(stories, now, fetch=web, cache_path=cache_path, out_path=out_path, **kw)
        return json.loads(out_path.read_text(encoding="utf-8")), json.loads(cache_path.read_text(encoding="utf-8")), stats


def test_a_news_outlets_image_is_never_used():
    big = picture(1600, 900)
    web = Web({"https://cdn.vox-cdn.com/a.jpg": (200, "image/jpeg", big),
               "https://arxiv.org/fig.png": (200, "image/png", big),
               "https://news.mit.edu/x.jpg": (200, "image/jpeg", big)})
    stories = [
        # A news outlet, even one the export wrongly called primary.
        story("verge", art("theverge.com", "https://cdn.vox-cdn.com/a.jpg", stype="press", lead=True)),
        story("verge-primary", art("theverge.com", "https://cdn.vox-cdn.com/a.jpg", stype="primary")),
        # Primary sources that are not a company: a preprint, a university news office.
        story("paper", art("arxiv.org", "https://arxiv.org/fig.png")),
        story("mit", art("news.mit.edu", "https://news.mit.edu/x.jpg")),
        # A company domain whose article the export did not mark primary.
        story("unmarked", art("openai.com", "https://cdn.vox-cdn.com/a.jpg", stype="press")),
        # A Hugging Face model card is anyone's upload; only the blog counts.
        story("hf-card", art("huggingface.co", "https://cdn.vox-cdn.com/a.jpg", url="https://huggingface.co/acme/model")),
    ]
    out, _, stats = run(stories, web)
    assert out == {}, out
    assert web.calls == [] and stats["candidates"] == 0, (web.calls, stats)
    # The company's post next to the outlet's: the company's picture is the one picked.
    s = story("mixed", art("theverge.com", "https://cdn.vox-cdn.com/a.jpg", stype="press", lead=True),
              art("blog.google", "https://storage.googleapis.com/g.png"))
    assert images.primary_candidate(s)["domain"] == "blog.google"
    assert images.company_announcement(art("huggingface.co", "x", url="https://huggingface.co/blog/smol"))
    assert images.company_announcement(art("aws.amazon.com", "x"))
    assert not images.company_announcement(art("github.com", "x"))


def test_a_primary_image_under_1200_px_is_not_used():
    web = Web({"https://openai.com/small.png": (200, "image/png", picture(1000, 525))})
    out, cache, stats = run([story("small", art("openai.com", "https://openai.com/small.png"))], web)
    assert out == {} and stats["checked"] == 1, (out, stats)
    assert cache["https://openai.com/small.png"]["width"] == 1000  # measured once, kept by URL
    # Not https: never checked, never used.
    web2 = Web({})
    out, _, _ = run([story("plain", art("openai.com", "http://openai.com/big.png"))], web2)
    assert out == {} and web2.calls == []


def test_a_good_primary_image_becomes_the_share_image_with_its_size():
    url = "https://images.ctfassets.net/openai/launch.jpg"
    web = Web({url: (200, "image/jpeg; charset=binary", picture(2400, 1260, "JPEG")),
               "https://blog.google/square.png": (200, "image/png", picture(1600, 1600))})
    stories = [story("launch", art("theverge.com", "https://cdn.vox-cdn.com/a.jpg", stype="press", lead=True),
                     art("openai.com", url)),
               story("square", art("blog.google", "https://blog.google/square.png"))]
    out, cache, stats = run(stories, web)
    assert out["launch"] == {"url": url, "width": 2400, "height": 1260, "ideal": True}, out
    # 1:1 is wide enough for og but not the ideal shape: used, marked so our card leads the NewsArticle images.
    assert out["square"] == {"url": "https://blog.google/square.png", "width": 1600, "height": 1600, "ideal": False}
    assert stats == {"candidates": 2, "checked": 2, "failed": 0, "used": 2, "ideal": 1}, stats
    # The next run within a day reads the cache: no request at all.
    web.calls.clear()
    out2, _, stats2 = run(stories, web, cache=cache, now=NOW + timedelta(hours=3))
    assert out2 == out and web.calls == [] and stats2["checked"] == 0


def test_the_card_comes_back_when_a_later_check_fails():
    url = "https://blogs.nvidia.com/hero.jpg"
    stories = [story("gpu", art("blogs.nvidia.com", url))]
    ok = Web({url: (200, "image/jpeg", picture(1920, 1080, "JPEG"))})
    out, cache, _ = run(stories, ok)
    assert out["gpu"]["width"] == 1920
    # A day later the picture is gone (404), then answers with a web page, then moves off https.
    for page in [(404, "text/html", b""), (200, "text/html", b"<html>"), (200, "image/svg+xml", b"<svg/>")]:
        gone = Web({url: page})
        out2, cache2, stats = run(stories, gone, cache=cache, now=NOW + timedelta(hours=25))
        assert out2 == {} and stats["failed"] == 1, (page, out2, stats)
        assert cache2[url]["ok"] is False
    # Not due yet and out of budget: a story whose picture was never checked keeps our card.
    out3, _, stats3 = run([story("new", art("openai.com", "https://openai.com/new.png"))], Web({}), limit=0)
    assert out3 == {} and stats3["checked"] == 0
    # A picture whose header cannot be read is not used either.
    broken = Web({url: (200, "image/jpeg", b"\xff\xd8\xff" + b"\0" * 100)})
    out4, _, _ = run(stories, broken)
    assert out4 == {}


def test_the_budget_takes_the_newest_stories_first():
    web = Web({f"https://openai.com/{i}.png": (200, "image/png", picture(1200, 630)) for i in range(4)})
    stories = [story(f"s{i}", art("openai.com", f"https://openai.com/{i}.png"), first=f"2026-09-2{i}T00:00:00Z") for i in range(4)]
    out, _, stats = run(stories, web, limit=2)
    assert sorted(out) == ["s2", "s3"] and stats["checked"] == 2, (out, stats)


def _meta(page: str, attr: str, name: str) -> str | None:
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else None


def test_built_story_pages_use_the_company_image_or_our_card():
    dist = SITE / "dist" / "story"
    listed_file = SITE / "src" / "data" / images.PRIMARY_FILE
    pages = sorted(dist.glob("*.html")) if dist.exists() else []  # build.format "file": story/<slug>.html
    if not pages:
        print("SKIP site not built")
        return
    listed = json.loads(listed_file.read_text(encoding="utf-8")) if listed_file.exists() else {}
    seen_primary = 0
    for page in pages:
        slug, text = page.stem, page.read_text(encoding="utf-8")
        if 'http-equiv="refresh"' in text:
            continue  # a redirect page
        og, tw = _meta(text, "property", "og:image"), _meta(text, "name", "twitter:image")
        w, h = _meta(text, "property", "og:image:width"), _meta(text, "property", "og:image:height")
        assert og and og == tw, (slug, og, tw)
        p = listed.get(slug)
        if p and p.get("width", 0) >= 1200:
            seen_primary += 1
            assert og == p["url"] and (w, h) == (str(p["width"]), str(p["height"])), (slug, og, w, h, p)
            docs = []
            for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S):
                ld = json.loads(block)
                docs += ld if isinstance(ld, list) else [ld]
            art_doc = next(d for d in docs if d.get("@type") == "NewsArticle")
            urls = [i["url"] for i in art_doc["image"]]
            assert p["url"] in urls
            assert (urls[0] == p["url"]) == bool(p.get("ideal")), (slug, urls, p)
        else:
            assert "digestai.news" in og and og.endswith(".png"), (slug, og)
            assert (w, h) != (None, None)
    print(f"checked {len(pages)} built story pages, {seen_primary} with the company's image")


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
