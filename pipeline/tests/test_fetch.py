"""Feed parsing (fetch.py): feeds with broken XML, aggregator items whose article is inside the
description (Techmeme), and the fallback feed. Offline: python tests/test_fetch.py"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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


def test_sources_yaml_is_consistent():
    """Every key unique, every typed key a listed source, weights in range, and none of the feeds that
    were found dead on 26 Nov 2026 (stale mirrors, pages that always answer 403/429) read again."""
    cfg = fetch.sources_yaml()
    keys = [s["key"] for s in cfg["sources"]]
    assert len(keys) == len(set(keys)), "duplicate keys"
    typed = [k for ks in cfg["types"].values() for k in ks]
    assert len(typed) == len(set(typed)), "a key typed twice"
    assert not set(typed) - set(keys), f"typed but not listed: {set(typed) - set(keys)}"
    for s in cfg["sources"]:
        assert s.get("category") and 0 < float(s.get("weight", 1.0)) <= 2, s["key"]
        assert s["url"].startswith("https://"), s["key"]
        assert isinstance(s.get("stale_days", 1), int), s["key"]
        for dead in ("rsshub.bestblogs.dev", "tim-hilde.github.io", "producthunt.com", "venturebeat.com/category",
                     "artificialintelligence-news.com/feed"):
            assert dead not in s["url"], (s["key"], dead)
    # The labs' own news is read, and typed primary so the coverage bar counts it.
    for key in ("anthropic", "anthropic-eng", "anthropic-research", "openai", "gemini", "meta-engineering"):
        assert key in keys and key in cfg["types"]["primary"], key
    marketing = [s for s in cfg["sources"] if s["category"] == "marketing"]
    assert len(marketing) >= 20, len(marketing)
    # Marketing feeds from general blogs keep only their AI posts.
    assert all(s.get("ai_only") for s in marketing if s["key"] != "marketing-ai-institute")


def test_source_changed_only_when_the_yaml_differs():
    item = {"key": "x", "name": "X", "url": "https://x.test/feed", "weight": 1.1, "category": "models"}
    values = fetch.curated_values(item, {"x": "primary"})
    assert values["source_type"] == "primary" and values["kind"] == "rss" and values["weight"] == 1.1
    row = NS(**values)
    assert not fetch.source_changed(row, values)
    assert fetch.source_changed(NS(**{**values, "weight": 0.9}), values)
    assert fetch.source_changed(NS(**{**values, "url": "https://x.test/old"}), values)
    assert fetch.source_changed(NS(**{**values, "enabled": False}), values)  # re-listed after removal
    assert not fetch.source_changed(NS(**{**values, "weight": 1.1000000000001}), values)


def test_backoff_rests_failing_sources_and_429s_at_once():
    assert fetch.backoff_hours(0, None) == 0
    assert fetch.backoff_hours(1, "ReadTimeout") == 0 and fetch.backoff_hours(2, "404 Client Error") == 0
    assert fetch.backoff_hours(3, "404 Client Error") == 1
    assert fetch.backoff_hours(5, "404 Client Error") == 4
    assert fetch.backoff_hours(535, "404 Client Error: Not Found") == fetch.MAX_BACKOFF_HOURS
    # 429: the publisher asked us to slow down, so the first one already rests an hour.
    assert fetch.backoff_hours(1, "429 Client Error: Too Many Requests for url: https://martech.org/feed/") == 1
    assert fetch.backoff_hours(3, "429 Client Error: Too Many Requests") == 4
    now = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    reddit = NS(error_count=535, last_error="404 Client Error", last_fetched_at=now - timedelta(hours=5))
    assert fetch.resting(reddit, now)
    assert not fetch.resting(NS(**{**vars(reddit), "last_fetched_at": now - timedelta(hours=25)}), now)
    assert not fetch.resting(NS(error_count=0, last_error=None, last_fetched_at=now), now)
    assert not fetch.resting(NS(error_count=7, last_error="x", last_fetched_at=None), now)  # never tried
    # Naive timestamps (SQLite) are read as UTC.
    assert fetch.resting(NS(error_count=1, last_error="429 Too Many", last_fetched_at=(now - timedelta(minutes=20)).replace(tzinfo=None)), now)


def _http_error(status: int):
    resp = NS(status_code=status)
    return fetch.requests.HTTPError(f"{status} Client Error", response=resp)


def test_a_429_stops_the_rest_of_that_host_for_the_run():
    srcs = [NS(key="a1", url="https://a.test/1", kind="rss"), NS(key="b1", url="https://b.test/1", kind="rss"),
            NS(key="a2", url="https://a.test/2", kind="rss"), NS(key="a3", url="https://a.test/3", kind="rss")]
    asked = []

    def rss(source):
        asked.append(source.key)
        if source.key == "a1":
            raise _http_error(429)
        return [{"url": source.url + "/item"}]

    out = fetch.fetch_all(srcs, fetchers={"rss": rss}, sleep=lambda _s: None)
    assert [s.key for s, _i, _e in out] == ["a1", "b1", "a2", "a3"]  # original order kept
    by_key = {s.key: (items, exc) for s, items, exc in out}
    assert fetch.rate_limited(by_key["a1"][1])
    assert isinstance(by_key["a2"][1], fetch.Skipped) and isinstance(by_key["a3"][1], fetch.Skipped)
    assert by_key["b1"][0] and by_key["b1"][1] is None
    assert sorted(asked) == ["a1", "b1"]
    # Other errors do not pause the host.
    asked.clear()

    def rss404(source):
        asked.append(source.key)
        raise _http_error(404)

    fetch.fetch_all(srcs, fetchers={"rss": rss404}, sleep=lambda _s: None)
    assert sorted(asked) == ["a1", "a2", "a3", "b1"]
    assert not fetch.rate_limited(_http_error(404)) and not fetch.rate_limited("404 Not Found for /429")


def test_health_notes_flag_empty_and_stale_feeds_without_failing_them():
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    fresh = fetch.FeedItems([{"published_at": now - timedelta(days=2)}])
    fresh.entries, fresh.newest = 10, now - timedelta(days=2)
    assert _with_cfg({"key": "k"}, lambda: fetch.health_note("k", fresh, now)) is None
    stale = fetch.FeedItems()
    stale.entries, stale.newest = 10, datetime(2026, 10, 17, tzinfo=timezone.utc)
    # The default allows 60 days; the Anthropic feed allows 21, so its stop on 17 Oct shows.
    assert _with_cfg({"key": "k"}, lambda: fetch.health_note("k", stale, now)) is None
    note = _with_cfg({"key": "k", "stale_days": 21}, lambda: fetch.health_note("k", stale, now))
    assert note == "stale: newest entry 2026-10-17 (40 days old)", note
    empty = fetch.FeedItems()
    assert _with_cfg({"key": "k"}, lambda: fetch.health_note("k", empty, now)).startswith("empty")
    # An ai_only feed can filter every entry out: that is not "empty".
    filtered = fetch.FeedItems()
    filtered.entries, filtered.newest = 12, now - timedelta(days=1)
    assert _with_cfg({"key": "k"}, lambda: fetch.health_note("k", filtered, now)) is None
    # Plain lists (Hacker News, Mastodon) are judged by their own items.
    assert _with_cfg({"key": "hn"}, lambda: fetch.health_note("hn", [{"published_at": now}], now)) is None


def test_items_are_taken_newest_first():
    d = lambda day: datetime(2026, 11, day, tzinfo=timezone.utc)  # noqa: E731
    items = [{"u": 1, "published_at": d(1)}, {"u": 2, "published_at": None}, {"u": 3, "published_at": d(20)},
             {"u": 4, "published_at": d(10).replace(tzinfo=None)}]
    assert [i["u"] for i in fetch.newest_first(items)] == [3, 4, 1, 2]


FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>EU AI Act</title>
<item><title>The EU AI Act Newsletter #90: omnibus delays</title>
<link>https://artificialintelligenceact.substack.com/p/the-eu-ai-act-newsletter-90</link>
<pubDate>Mon, 24 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>The EU AI Act Newsletter #89: codes of practice</title>
<link>https://artificialintelligenceact.substack.com/p/the-eu-ai-act-newsletter-89</link>
<pubDate>Mon, 10 Nov 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


class _Resp:
    def __init__(self, status, content):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _http_error(self.status_code)


def _with_get(get, fn):
    saved = fetch.SESSION.get
    fetch.SESSION.get = get
    try:
        return fn()
    finally:
        fetch.SESSION.get = saved


def test_keep_domain_filters_only_the_fallback_feed():
    cfg = {"key": "eu-ai-act", "url": "https://artificialintelligenceact.substack.com/feed",
           "fallback": "https://www.bing.com/news/search?q=x&format=rss", "keep_domain": "artificialintelligenceact.eu"}
    src = NS(key="eu-ai-act", url=cfg["url"], kind="rss")
    items = _with_get(lambda url, **_k: _Resp(200, FEED), lambda: _with_cfg(cfg, lambda: fetch._rss_items(src)))
    assert len(items) == 2 and items.entries == 2
    assert items.newest == datetime(2026, 11, 24, 8, tzinfo=timezone.utc)


def test_an_empty_main_feed_falls_back():
    """AI News served its HTML at /feed/ with status 200: that reads as no entries, and a source with a
    fallback then reads the fallback instead of reporting nothing."""
    cfg = {"key": "s", "url": "https://site.test/feed/", "fallback": "https://fallback.test/rss"}
    src = NS(key="s", url=cfg["url"], kind="rss")
    calls = []

    def get(url, **_k):
        calls.append(url)
        return _Resp(200, b"<html><body>Our site</body></html>") if "site.test" in url else _Resp(200, FEED)

    items = _with_get(get, lambda: _with_cfg(cfg, lambda: fetch._rss_items(src)))
    assert calls == [cfg["url"], cfg["fallback"]] and len(items) == 2
    # Without a fallback an empty feed is simply empty (health_note then says so).
    items = _with_get(get, lambda: _with_cfg({"key": "s", "url": cfg["url"]}, lambda: fetch._rss_items(src)))
    assert len(items) == 0


def test_outcomes_are_written_in_one_statement():
    from sqlalchemy import create_engine, insert, select

    from digest import db

    eng = create_engine("sqlite://", future=True)
    db.sources.create(eng)
    with eng.begin() as conn:
        for i, key in enumerate(("ok", "bad", "stale"), start=1):
            conn.execute(insert(db.sources).values(id=i, key=key, name=key, url=f"https://{key}.test", error_count=4,
                                                   last_error="old"))
    statements = []
    from sqlalchemy import event

    event.listen(eng, "before_cursor_execute", lambda *a, **k: statements.append(a[2]))
    when = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    fetch.record_outcomes(eng, [{"sid": 1, "err": None, "cnt": 0}, {"sid": 2, "err": "404 Client Error", "cnt": 5},
                                {"sid": 3, "err": "stale: newest entry 2026-10-17 (40 days old)", "cnt": 0}], when=when)
    assert len([s for s in statements if s.lstrip().upper().startswith("UPDATE")]) == 1, statements
    with eng.connect() as conn:
        rows = {r.key: r for r in conn.execute(select(db.sources)).all()}
    assert rows["ok"].error_count == 0 and rows["ok"].last_error is None
    assert rows["bad"].error_count == 5 and rows["bad"].last_error.startswith("404")
    assert rows["stale"].error_count == 0 and rows["stale"].last_error.startswith("stale:")
    assert db.as_utc(rows["ok"].last_fetched_at) == when
    fetch.record_outcomes(eng, [])  # nothing fetched: nothing written


def test_extract_defers_a_publisher_that_answered_429():
    from digest import extract

    slowed: set[str] = set()
    assert not extract.defer_for_rate_limit("venturebeat.com", slowed)
    slowed.add("venturebeat.com")
    assert extract.defer_for_rate_limit("venturebeat.com", slowed)
    assert not extract.defer_for_rate_limit("techcrunch.com", slowed) and not extract.defer_for_rate_limit("", slowed)


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
