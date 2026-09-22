"""Feed parsing (fetch.py): feeds with broken XML, aggregator items whose article is inside the
description (Techmeme), and the fallback feed. Offline: python tests/test_fetch.py"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import fetch  # noqa: E402
from digest.textutil import domain_of, normalize_url  # noqa: E402

# Two items as Techmeme's feed.xml carried them on 21 Sep 2026 (the image tags trimmed).
TECHMEME = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Techmeme</title>
<link>https://www.techmeme.com/</link>
<item>
  <title>Xiaomi debuts open-weight omnimodal models MiMo-V2.6 Pro and Flash; Pro allegedly performs &quot;on par with Opus 5 and GPT-5.6 Sol across most agent benchmarks&quot; (Xiaomi)</title>
  <link>https://www.techmeme.com/260921/p52#a260921p52</link>
  <description><![CDATA[<P><A HREF="https://www.techmeme.com/260921/p52#a260921p52" TITLE="Techmeme permalink"><IMG WIDTH=11 HEIGHT=12 SRC="http://www.techmeme.com/img/pml.png"></A> <A HREF="https://mimo.xiaomi.com/">Xiaomi</A>:<BR>
<SPAN STYLE="font-size:1.3em;"><B><A HREF="https://mimo.xiaomi.com/mimo-v2-6">Xiaomi debuts open-weight omnimodal models MiMo-V2.6 Pro and Flash</A></B></SPAN>&nbsp; &mdash;&nbsp; Frontier intelligence, all the modalities, built in public.</P>
]]></description>
  <pubDate>Mon, 21 Sep 2026 18:10:02 -0400</pubDate>
  <guid>https://www.techmeme.com/260921/p52#a260921p52</guid>
</item>
<item>
  <title>Some startups, like Harvey, Abridge, Ramp, and Rogo, are embracing open-weight models or training their own models to reduce expensive reliance on frontier labs (Bloomberg)</title>
  <link>https://www.techmeme.com/260921/p51#a260921p51</link>
  <description><![CDATA[<A HREF="https://www.bloomberg.com/news/articles/2026-09-21/startups-like-harvey-embrace-open-models-to-cut-reliance-on-anthropic-openai?accessToken=eyJhbGciOi"><IMG SRC="http://www.techmeme.com/260921/i51.jpg"></A>
<P><A HREF="https://www.techmeme.com/260921/p51#a260921p51" TITLE="Techmeme permalink"></A> <A HREF="https://www.bloomberg.com/">Bloomberg</A>:<BR>
<SPAN STYLE="font-size:1.3em;"><B><A HREF="https://www.bloomberg.com/news/articles/2026-09-21/startups-like-harvey-embrace-open-models-to-cut-reliance-on-anthropic-openai?accessToken=eyJhbGciOi">Some startups embrace open-weight models</A></B></SPAN> &mdash; AI startups cut their reliance on OpenAI and Anthropic.</P>
]]></description>
  <pubDate>Mon, 21 Sep 2026 17:55:02 -0400</pubDate>
</item>
<item>
  <title>Apple opens a new store in Milan with a rooftop garden (The Verge)</title>
  <link>https://www.techmeme.com/260921/p50#a260921p50</link>
  <description><![CDATA[<P><A HREF="https://www.theverge.com/">The Verge</A>: <A HREF="https://www.theverge.com/2026/9/21/apple-milan-store">Apple opens a new store in Milan</A> &mdash; A rooftop garden and a forum.</P>]]></description>
  <pubDate>Mon, 21 Sep 2026 17:40:00 -0400</pubDate>
</item>
</channel>
</rss>
"""


def _with_cfg(cfg: dict, fn):
    saved = fetch._SOURCES_YAML
    fetch._SOURCES_YAML = {"sources": [cfg]}
    try:
        return fn()
    finally:
        fetch._SOURCES_YAML = saved


def test_techmeme_items_store_the_original_article_and_credit_the_outlet():
    cfg = {"key": "techmeme", "url": "https://www.techmeme.com/feed.xml", "link_from": "description", "ai_only": True}
    src = NS(key="techmeme", url=cfg["url"], kind="rss")
    items = _with_cfg(cfg, lambda: fetch._entry_items(src, fetch.parse_feed(TECHMEME)))
    urls = [normalize_url(i["url"]) for i in items]
    # The headline link, not the permalink and not the outlet's home page; the store in Milan is not about AI.
    assert [domain_of(u) for u in urls] == ["mimo.xiaomi.com", "bloomberg.com"], urls
    assert urls[0] == "https://mimo.xiaomi.com/mimo-v2-6"
    assert "techmeme" not in " ".join(urls)
    assert items[1]["title"].endswith("reliance on frontier labs")  # "(Bloomberg)" taken off
    assert items[0]["description"].startswith("Xiaomi :") or "Frontier intelligence" in items[0]["description"]
    assert "<" not in items[0]["description"] and items[0]["published_at"].year == 2026
    assert items[0]["image_url"] is None and items[0]["feed_content"] is None


def test_link_in_description_skips_permalinks_and_home_pages():
    html = '<A HREF="https://www.techmeme.com/x#a">p</A> <A HREF="https://www.ft.com/">FT</A> <A HREF="https://www.ft.com/content/1">story</A>'
    assert fetch.link_in_description(html, "techmeme.com") == "https://www.ft.com/content/1"
    assert fetch.link_in_description('<a href="mailto:x@y.z">m</a>', "techmeme.com") is None
    assert fetch.link_in_description("", "techmeme.com") is None


def test_broken_xml_is_repaired_before_giving_up():
    good = b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>AI News</title>' \
           b'<item><title>R&D spend on AI\x0b agents rises</title><link>https://www.artificialintelligence-news.com/a/</link>' \
           b'<description><![CDATA[Q&A with a lab & more]]></description></item></channel></rss>'
    broken = b"<br />\n<b>Warning</b>: Undefined index in functions.php\n" + good
    assert fetch.feedparser.parse(broken).bozo
    feed = fetch.parse_feed(broken)
    assert len(feed.entries) == 1 and feed.entries[0].title == "R&D spend on AI agents rises"
    fixed = fetch.repair_xml(broken)
    assert fixed.startswith(b"<?xml") and b"\x0b" not in fixed
    assert b"<![CDATA[Q&A with a lab & more]]>" in fixed  # CDATA is left alone
    assert b"R&amp;D" in fixed
    # A feed that parses is never touched.
    assert fetch.parse_feed(TECHMEME).entries[0].link.startswith("https://www.techmeme.com/")


def test_a_failing_feed_falls_back_and_keeps_only_its_own_domain():
    bing = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Bing</title>
<item><title>Alibaba plans a 10-trillion-parameter AI model</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=1&amp;url=https%3a%2f%2fwww.unite.ai%2falibaba-plans%2f&amp;c=9</link></item>
<item><title>Palo Alto AI startup raises $85M</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=1&amp;url=https%3a%2f%2fhoodline.com%2f2026%2f09%2fx%2f&amp;c=9</link></item>
</channel></rss>"""

    class Resp:
        def __init__(self, status, content):
            self.status_code, self.content = status, content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise fetch.requests.HTTPError(f"{self.status_code}")

    calls = []

    def get(url, **_kw):
        calls.append(url)
        return Resp(503, b"<html>blocked</html>") if "unite.ai/feed" in url else Resp(200, bing)

    cfg = {"key": "unite-ai", "url": "https://www.unite.ai/feed/", "fallback": "https://www.bing.com/news/search?q=%22unite.ai%22&format=rss",
           "keep_domain": "unite.ai"}
    src = NS(key="unite-ai", url=cfg["url"], kind="rss")
    saved = fetch.SESSION.get
    fetch.SESSION.get = get
    try:
        items = _with_cfg(cfg, lambda: fetch._rss_items(src))
        assert calls == [cfg["url"], cfg["fallback"]]
        assert [normalize_url(i["url"]) for i in items] == ["https://unite.ai/alibaba-plans"], items
        # Without a fallback the error still reaches the run, which counts it against the source.
        try:
            _with_cfg({"key": "unite-ai", "url": cfg["url"]}, lambda: fetch._rss_items(src))
            raise AssertionError("expected the 503 to be raised")
        except fetch.requests.HTTPError:
            pass
    finally:
        fetch.SESSION.get = saved


def test_sources_yaml_has_the_techmeme_source_as_community():
    cfg = fetch.sources_yaml()
    tm = next(s for s in cfg["sources"] if s["key"] == "techmeme")
    assert tm["link_from"] == "description" and tm["ai_only"] and "techmeme" in cfg["types"]["community"]
    for s in cfg["sources"]:
        assert "feedburner.com/venturebeat" not in s["url"]


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
