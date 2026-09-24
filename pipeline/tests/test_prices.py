"""AI at Work price history (/work/prices): the store, the comparison, the maker's own pricing page
and the page's thin rule. Offline: python tests/test_prices.py"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import prices, work  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
SITE = Path(__file__).resolve().parents[2] / "site"


def price(amount=12, currency="USD", period="month", plan="Pro", free_limit="", quoted="Pro costs $12 a month.") -> dict:
    return {"plan": plan, "amount": amount, "currency": currency, "period": period,
            "free_limit": free_limit, "quoted": quoted}


def obs(at: str, **over) -> dict:
    return prices.observation(price(**over), at, source_url="https://canva.com/pricing", story_slug="a-story")


def store_with(*rows) -> dict:
    store = prices._empty_store()
    for row in rows:
        prices.record(store, "canva|canva", "Canva Pro", "Canva", row)
    return store


# ---------------------------------------------------------------------------- the store

def test_a_price_that_did_not_change_is_not_a_second_row():
    store = store_with(obs("2026-08-01T09:00:00Z"), obs("2026-09-01T09:00:00Z"))
    seen = store["tools"]["canva|canva"]["observations"]
    assert len(seen) == 1, "the same price seen again is the same price"
    assert seen[0]["at"] == "2026-08-01T09:00:00Z", "when it started"
    assert seen[0]["checkedAt"] == "2026-09-01T09:00:00Z", "when it was last confirmed"
    # A different figure is a new row, and record() says so.
    assert prices.record(store, "canva|canva", "Canva Pro", "Canva", obs("2026-09-20T09:00:00Z", amount=15))
    assert len(store["tools"]["canva|canva"]["observations"]) == 2


def test_the_history_only_moves_forward_and_stays_bounded():
    store = store_with(obs("2026-09-01T09:00:00Z", amount=15))
    assert not prices.record(store, "canva|canva", "Canva Pro", "Canva", obs("2026-08-01T09:00:00Z", amount=9)), \
        "a price older than the one on record is not news"
    for n in range(prices.OBSERVATIONS_MAX + 6):
        prices.record(store, "canva|canva", "Canva Pro", "Canva",
                      obs(f"2026-10-{n + 1:02d}T09:00:00Z", amount=20 + n))
    assert len(store["tools"]["canva|canva"]["observations"]) == prices.OBSERVATIONS_MAX
    # A tool nobody has mentioned for KEEP_DAYS leaves the history.
    old = store_with(obs("2020-01-01T09:00:00Z"))
    assert prices.prune(old, NOW)["tools"] == {}
    assert prices.prune(store_with(obs("2026-09-01T09:00:00Z")), NOW)["tools"]


def test_the_store_survives_a_round_trip_and_a_broken_file():
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "work-prices.json.gz"
        store = store_with(obs("2026-08-01T09:00:00Z", amount=9), obs("2026-09-01T09:00:00Z", amount=12))
        prices.save(store, NOW, path)
        back = prices.load(path)
        assert back["tools"]["canva|canva"]["observations"] == store["tools"]["canva|canva"]["observations"]
        # A missing or corrupt file costs history, never correctness.
        assert prices.load(tmp / "nothing.json.gz") == {"format": prices.FORMAT, "tools": {}}
        (tmp / "junk.json.gz").write_bytes(b"not gzip")
        assert prices.load(tmp / "junk.json.gz")["tools"] == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prices_are_recorded_from_the_run_s_own_cards():
    card = work.card_out(work.clean_card({
        "fits": True, "tool": "Canva Magic Studio", "maker": "Canva",
        "what_it_does": "Writes and lays out social posts from a short brief.",
        "who_for": ["marketer"], "use_for": ["Draft a week of posts", "Resize one ad", "Write captions"],
        "cost": "paid from $12 a month", "effort": "minutes", "watch_out": "The free tier watermarks video.",
        "link": "https://www.canva.com/magic-studio/",
        "price": {"plan": "Pro", "amount": 12, "currency": "USD", "period": "month",
                  "quoted": "Canva Pro costs $12 a month."},
    }))
    story = {"id": 1, "slug": "canva-raises-pro", "firstPublishedAt": "2026-09-20T09:00:00Z", "workCard": card}
    store = prices._empty_store()
    stats = prices.record_from_stories(store, [story, {"id": 2, "slug": "no-card", "workCard": None}])
    assert stats == {"seen": 1, "changed": 1}
    row = store["tools"][card["toolKey"]]["observations"][0]
    assert row["amount"] == 12.0 and row["storySlug"] == "canva-raises-pro" and row["source"] == "article"
    assert row["sourceUrl"] == "https://www.canva.com/magic-studio/"
    # A card with no price is not an observation: the history only ever holds grounded figures.
    assert prices.record_from_stories(prices._empty_store(), [{"id": 3, "slug": "x", "workCard": {"tool": "T"}}]) == \
        {"seen": 0, "changed": 0}


# ---------------------------------------------------------------------------- what changed

def test_was_says_what_it_cost_and_when():
    store = store_with(obs("2026-08-14T09:00:00Z", amount=12), obs("2026-09-20T09:00:00Z", amount=19))
    seen = store["tools"]["canva|canva"]["observations"]
    assert prices.was_line(seen) == "was $12/mo in August"
    assert prices.was_line(seen[:1]) == "", "one price is not a comparison"
    # A year that is not this one is named, so "August" is never two Augusts.
    old = store_with(obs("2024-08-14T09:00:00Z", amount=12), obs("2026-09-20T09:00:00Z", amount=19))
    assert prices.was_line(old["tools"]["canva|canva"]["observations"]) == "was $12/mo in August 2024"
    # When the money did not move and the free tier did, saying "was $12/mo" tells the reader nothing.
    free = store_with(obs("2026-08-14T09:00:00Z", amount=12, free_limit="100 images"),
                      obs("2026-09-20T09:00:00Z", amount=12, free_limit="50 images"))
    assert prices.was_line(free["tools"]["canva|canva"]["observations"]) == "free tier was 100 images in August"
    opened = store_with(obs("2026-08-14T09:00:00Z", amount=12),
                        obs("2026-09-20T09:00:00Z", amount=12, free_limit="50 images"))
    assert prices.was_line(opened["tools"]["canva|canva"]["observations"]) == "no free tier in August"


def test_what_counts_as_a_change():
    up = prices.change_kind(obs("a", amount=12), obs("b", amount=19))
    down = prices.change_kind(obs("a", amount=19), obs("b", amount=12))
    opened = prices.change_kind(obs("a", amount=19), obs("b", amount=19, free_limit="500 images a month"))
    free = prices.change_kind(obs("a", amount=19, free_limit="100 images"), obs("b", amount=19, free_limit="50 images"))
    went_free = prices.change_kind(obs("a", amount=19), obs("b", amount=0))
    assert (up, down, opened, free, went_free) == ("rose", "fell", "opened", "free", "opened")
    assert prices.change_kind(obs("a"), obs("b")) is None, "the same price is not a change"
    # A different currency is not a rise: there is nothing to compare.
    assert prices.change_kind(obs("a", amount=12, currency="USD"), obs("b", amount=14, currency="EUR")) is None


def test_only_a_tool_with_two_observations_is_on_the_page():
    one = store_with(obs("2026-09-20T09:00:00Z"))
    assert prices.changes(one) == [], "one price is not movement"
    moved = store_with(obs("2026-08-14T09:00:00Z", amount=12), obs("2026-09-20T09:00:00Z", amount=19))
    change = prices.changes(moved)[0]
    assert change["kind"] == "rose" and change["was"] == "was $12/mo in August"
    assert change["now"]["quoted"] == "Pro costs $12 a month." and change["at"] == "2026-09-20T09:00:00Z"
    assert change["tool"] == "Canva Pro" and change["maker"] == "Canva"


def test_the_export_carries_the_history_and_the_changes():
    tmp = Path(tempfile.mkdtemp())
    try:
        store = store_with(obs("2026-08-14T09:00:00Z", amount=12), obs("2026-09-20T09:00:00Z", amount=19))
        path = tmp / "work-prices.json"
        assert prices.write_export(store, NOW, path) == 1
        data = json.loads(path.read_text(encoding="utf-8"))
        tool = data["tools"][0]
        assert tool["current"]["amount"] == 19.0 and tool["was"] == "was $12/mo in August"
        assert tool["checkedAt"] == "2026-09-20T09:00:00Z" and len(tool["observations"]) == 2
        assert [c["kind"] for c in data["changes"]] == ["rose"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------- the maker's own page

PRICING_PAGE = (
    "Canva pricing. Canva Free is free forever and includes 5 GB of storage. "
    "Canva Pro costs $15 a month for one person, billed monthly, or $120 a year. "
    "Canva Teams starts at $10 per person a month with a minimum of three people. "
    "Every plan includes the design editor, templates and the brand kit, and you can cancel at any time."
)


def card_for(link="https://www.canva.com/magic-studio/", **over) -> dict:
    base = {"tool": "Canva Magic Studio", "maker": "Canva", "link": link, "price": None}
    base.update(over)
    return base


def test_only_the_makers_own_page_is_read():
    assert prices.fetchable(card_for(), {"articles": []}) is None
    assert prices.fetchable(card_for(price={"amount": 12}), {"articles": []}) == "has a price"
    assert prices.fetchable(card_for("https://techcrunch.com/canva"), {"articles": []}) == "news"
    assert prices.fetchable(card_for("https://github.com/canva"), {"articles": []}) == "community"
    assert prices.fetchable(card_for("https://someoneelse.example/canva"), {"articles": []}) == "not the maker"
    assert prices.fetchable(card_for(None), {"articles": []}) == "no link"
    # The addresses tried, in order, all on the tool's own host and nowhere else.
    urls = prices.pricing_urls("https://www.canva.com/magic-studio/")
    assert urls[0] == "https://canva.com/pricing" and len(urls) == len(prices.PRICING_PATHS)
    assert all(u.startswith("https://canva.com/") for u in urls), "never off the tool's own host"
    assert prices.pricing_urls("http://www.canva.com/x") == [], "https only"


def test_only_what_the_pricing_page_states_is_kept():
    answer = {"price": {"plan": "Pro", "amount": 15, "currency": "USD", "period": "month", "free_limit": "5 GB of storage",
                        "quoted": "Canva Pro costs $15 a month for one person"}}
    got, status = prices.read_price(card_for(), "https://www.canva.com/pricing",
                                    fetch=lambda _u: PRICING_PAGE, ask=lambda _p: answer)
    assert status == "ok" and got["amount"] == 15.0 and got["period"] == "month"
    assert got["quoted"] == "Canva Pro costs $15 a month for one person"
    # A figure the page does not state drops the whole price, exactly as it does for an article.
    invented = {"price": {"plan": "Pro", "amount": 13, "currency": "USD", "period": "month", "quoted": "Pro is $13 a month"}}
    assert prices.read_price(card_for(), "https://www.canva.com/pricing",
                             fetch=lambda _u: PRICING_PAGE, ask=lambda _p: invented) == (None, "none")
    # A page that cannot be read, and a model that will not answer, are both "failed".
    assert prices.read_price(card_for(), "https://x/", fetch=lambda _u: None, ask=lambda _p: answer)[1] == "failed"
    assert prices.read_price(card_for(), "https://x/", fetch=lambda _u: PRICING_PAGE,
                             ask=lambda _p: (_ for _ in ()).throw(RuntimeError("no model")))[1] == "failed"
    # A page too short to check anything against is not read for a price either.
    assert prices.read_price(card_for(), "https://x/", fetch=lambda _u: "Pricing.", ask=lambda _p: answer)[1] == "failed"


def story_for(sid: int, tool: str, link: str | None, maker: str = "Canva") -> dict:
    card = {"tool": tool, "maker": maker, "link": link, "price": None, "toolKey": work.tool_key(tool, maker)}
    return {"id": sid, "slug": f"story-{sid}", "firstPublishedAt": f"2026-09-{20 + sid % 8:02d}T09:00:00Z",
            "workCard": card, "articles": []}


ANSWER = {"price": {"plan": "Pro", "amount": 15, "currency": "USD", "period": "month",
                    "quoted": "Canva Pro costs $15 a month for one person"}}


def test_the_step_reads_at_most_a_handful_of_pages_a_run():
    # Seven tools, each with its own maker and its own site: nothing can be shared between them.
    makers = ["Makerone", "Makertwo", "Makerthree", "Makerfour", "Makerfive", "Makersix", "Makerseven"]
    stories = [story_for(i, f"Widget {i}", f"https://{m.lower()}.com/product", m) for i, m in enumerate(makers, 1)]
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        return PRICING_PAGE

    store, cache = prices._empty_store(), {}
    stats = prices.run(stories, fetch=fetch, ask=lambda _p: ANSWER, now=NOW, limit=3, store=store, page_cache=cache)
    assert stats["candidates"] == 7, "one per tool, all with a maker page"
    assert stats["fetched"] == 3 and len(calls) == 3, "the run's cap holds"
    assert stats["found"] == 3 and stats["recorded"] == 3, "the four that were not read cost nothing"
    # A page already read is reused rather than fetched again, and the rest wait for the next run.
    again = prices.run(stories, fetch=fetch, ask=lambda _p: ANSWER, now=NOW, limit=3, store=store, page_cache=cache)
    assert again["reused"] == 3 and again["fetched"] == 3 and len(calls) == 6
    # A run with no time left reads nothing at all.
    none = prices.run(stories, fetch=fetch, ask=lambda _p: ANSWER, now=NOW, limit=3, budget_seconds=-1,
                      store=prices._empty_store(), page_cache={})
    assert none["fetched"] == 0 and len(calls) == 6


def test_one_page_answers_for_every_tool_on_the_same_site():
    stories = [story_for(i, f"Canva Thing {i}", "https://www.canva.com/magic-studio/") for i in range(1, 8)]
    calls: list[str] = []
    stats = prices.run(stories, fetch=lambda u: calls.append(u) or PRICING_PAGE, ask=lambda _p: ANSWER,
                       now=NOW, limit=3, store=prices._empty_store(), page_cache={})
    assert len(calls) == 1 and stats["fetched"] == 1 and stats["reused"] == 6
    assert stats["recorded"] == 7, "every tool on that site gets the price the one page gave"


def test_a_page_with_no_price_is_not_fetched_again_in_the_same_run():
    stories = [story_for(i, f"Canva Thing {i}", "https://www.canva.com/magic-studio/") for i in range(1, 4)]
    calls: list[str] = []
    stats = prices.run(stories, fetch=lambda u: calls.append(u) or PRICING_PAGE, ask=lambda _p: {"price": None},
                       now=NOW, limit=len(prices.PRICING_PATHS), store=prices._empty_store(), page_cache={})
    assert len(calls) == len(prices.PRICING_PATHS), "every address is tried once, then the cap stops it"
    assert stats["none"] == len(prices.PRICING_PATHS) and stats["recorded"] == 0


def test_a_page_is_asked_again_only_when_it_is_due():
    clock = NOW.timestamp()
    assert prices._due(None, clock)
    assert not prices._due({"at": clock, "status": "ok"}, clock)
    assert prices._due({"at": clock - (prices.RECHECK_DAYS + 1) * 86400, "status": "ok"}, clock), "prices are re-read"
    assert not prices._due({"at": clock - 2 * 86400, "status": "failed"}, clock)
    assert prices._due({"at": clock - (prices.RETRY_DAYS + 1) * 86400, "status": "failed"}, clock)
    assert prices._due({"at": clock - (prices.NONE_DAYS + 1) * 86400, "status": "none"}, clock)


def test_a_run_with_its_own_store_never_touches_the_sites_copy():
    """The export file is the site's; only a real run rewrites it, and only where it is told to."""
    before = prices.EXPORT_FILE.read_bytes() if prices.EXPORT_FILE.exists() else None
    stories = [story_for(1, "Canva Thing", "https://www.canva.com/magic-studio/")]
    prices.run(stories, fetch=lambda _u: PRICING_PAGE, ask=lambda _p: ANSWER, now=NOW,
               store=prices._empty_store(), page_cache={})
    after = prices.EXPORT_FILE.read_bytes() if prices.EXPORT_FILE.exists() else None
    assert after == before, "a test run must not write into site/src/data"
    tmp = Path(tempfile.mkdtemp())
    try:
        out = tmp / "work-prices.json"
        stats = prices.run(stories, fetch=lambda _u: PRICING_PAGE, ask=lambda _p: ANSWER, now=NOW,
                           store=prices._empty_store(), page_cache={}, export_path=out)
        assert stats["tools"] == 1 and json.loads(out.read_text(encoding="utf-8"))["tools"][0]["current"]["amount"] == 15.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_card_that_states_its_own_price_is_never_fetched():
    priced = story_for(1, "Canva Magic Studio", "https://www.canva.com/magic-studio/")
    priced["workCard"]["price"] = {"amount": 12, "currency": "USD", "period": "month", "quoted": "x"}
    calls = []
    stats = prices.run([priced], fetch=lambda u: calls.append(u) or PRICING_PAGE, ask=lambda _p: {},
                       now=NOW, store=prices._empty_store(), page_cache={})
    assert stats["candidates"] == 0 and calls == []


# ---------------------------------------------------------------------------- the page's thin rule

def _node(script: str):
    lib = (SITE / "src" / "lib" / "indexing.mjs").as_uri()
    probe = f"import * as lib from {json.dumps(lib)};\n{script}"
    node = shutil.which("node")
    if not node:
        return None
    res = subprocess.run([node, "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    return json.loads(res.stdout)


def test_the_prices_page_is_indexable_only_once_enough_tools_have_moved():
    story = {"id": 1, "slug": "s", "importance": 7, "articleCount": 2, "hasPrimary": True, "pinned": False,
             "firstPublishedAt": "2026-09-20T08:00:00Z", "updatedAt": "2026-09-20T08:00:00Z",
             "articles": [{"domain": "a.com", "url": "https://a.com/1", "publishedAt": "2026-09-20T08:00:00Z"}],
             "workCard": {"tool": "T", "maker": "M", "jobs": ["business"]}}
    changes = [{"at": f"2026-09-2{n}T09:00:00Z"} for n in range(5)]
    script = """
const story = %s;
const few = %s.slice(0, 4);
const enough = %s;
const out = {
  min: lib.WORK_PRICES_MIN_ENTRIES,
  thin: [...lib.noindexPaths([story], [], [], few)].includes("/work/prices"),
  ok: [...lib.noindexPaths([story], [], [], enough)].includes("/work/prices"),
  none: [...lib.noindexPaths([story], [], [])].includes("/work/prices"),
  listed: lib.sitemapIndex({ stories: [story], priceChanges: enough }).include("/work/prices"),
  unlisted: lib.sitemapIndex({ stories: [story], priceChanges: few }).include("/work/prices"),
};
console.log(JSON.stringify(out));
""" % (json.dumps(story), json.dumps(changes), json.dumps(changes))
    out = _node(script)
    if out is None:
        print("SKIP node not on PATH")
        return
    assert out["min"] == 5
    assert out["thin"] is True, "four tools that moved is a stub"
    assert out["ok"] is False, "five is a page"
    assert out["none"] is True, "no history at all is a stub"
    assert out["listed"] is True and out["unlisted"] is False


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
