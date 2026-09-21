"""AI at Work: the order readers teach (work_learn.py). Shrinkage, the blend's cap, the hard rules,
recency, the one grouped read and its cache. Offline: python tests/test_work_learn.py"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from digest import db, morning, work, work_learn as wl  # noqa: E402
from test_work import NOW, card, with_card  # noqa: E402

TOOLS = [
    # (tool, maker, cost, effort)
    ("Canva Magic Studio", "Canva", "free", "minutes"),
    ("HubSpot Breeze", "HubSpot", "free tier", "minutes"),
    ("Shopify Magic", "Shopify", "free", "minutes"),
    ("Notion AI", "Notion", "paid from $10 a month", "minutes"),
    ("Gemini in Gmail", "Google", "free", "minutes"),
    ("Adobe Firefly", "Adobe", "paid from $5 a month", "minutes"),
]


def section() -> list[dict]:
    """Six cards published an hour apart, with official links and publisher coverage."""
    out = []
    for i, (tool, maker, cost, effort) in enumerate(TOOLS, 1):
        c = work.clean_card(card(tool=tool, maker=maker, cost=cost, effort=effort,
                                 link=f"https://www.{maker.lower()}.com/ai"))
        s = with_card(i, {i: c})
        s["articles"] = [{"id": i, "domain": "theverge.com"}]
        out.append(s)
    return out


def row(views: int, tries: int = 0, copies: int = 0, expands: int = 0, recency: float = 0.9) -> dict:
    """Counts as the read returns them, every event weighted `recency`."""
    return {"views": views, "try": tries, "copy_prompt": copies, "expand": expands,
            "wviews": views * recency, "wtry": tries * recency, "wcopy_prompt": copies * recency, "wexpand": expands * recency}


def order(stories, **kw) -> list[int]:
    return work.build_briefing(stories, NOW, **kw)["storyIds"]


def learned_order(by_tool: dict) -> tuple[list[dict], dict, list[int]]:
    stories = section()
    learned = wl.model(by_tool, stories)
    wl.apply(stories, learned)
    return stories, learned, order(stories)


# ---------------------------------------------------------------------------- the model

def test_without_data_the_order_is_exactly_the_rules_order():
    rules = order(section())
    stories, learned, got = learned_order({})
    assert not learned["active"] and got == rules
    assert all("rulesUsefulness" not in s["workCard"] for s in stories)


def test_tiny_data_does_not_move_the_order():
    rules = order(section())
    # 20 card views and a few tries, all on the last card: far below the 200 views learning needs.
    tiny = {wl.tool_key(t[0]): row(3) for t in TOOLS}
    tiny[wl.tool_key("Adobe Firefly")] = row(5, tries=3, copies=2)
    stories, learned, got = learned_order(tiny)
    assert learned["views"] == 20 and not learned["active"] and got == rules
    # Above the threshold, one lucky click on one card is still noise: shrinkage and the dead band.
    lucky = {wl.tool_key(t[0]): row(60, tries=2) for t in TOOLS}
    lucky[wl.tool_key("Adobe Firefly")] = row(60, tries=3)
    stories, learned, got = learned_order(lucky)
    assert learned["active"] and got == rules, (got, rules)
    assert all(s["workCard"]["usefulness"] == s["workCard"].get("rulesUsefulness", s["workCard"]["usefulness"])
               for s in stories), "one extra click moves no score at all"


def test_strong_evidence_moves_the_order():
    rules = order(section())
    last = rules[-1]
    tool = next(s for s in section() if s["id"] == last)["workCard"]["tool"]
    strong = {wl.tool_key(t[0]): row(400, tries=8) for t in TOOLS}
    strong[wl.tool_key(tool)] = row(400, tries=80, copies=20)
    stories, learned, got = learned_order(strong)
    assert learned["active"] and got != rules
    assert got.index(last) < rules.index(last), (got, rules)
    moved = next(s for s in stories if s["id"] == last)["workCard"]
    assert moved["usefulness"] > moved["rulesUsefulness"] and 0 < moved["learnedWeight"] <= wl.W_CAP


def test_the_learned_weight_is_capped_so_the_rules_keep_a_say():
    huge = {wl.tool_key(t[0]): row(100_000, tries=1_000) for t in TOOLS}
    huge[wl.tool_key("Adobe Firefly")] = row(100_000, tries=50_000)
    learned = wl.model(huge, section())
    assert all(c["w"] == wl.W_CAP for c in learned["cards"].values())
    top = learned["cards"][wl.tool_key("Adobe Firefly")]
    shift = wl.blended(5.0, top) - 5.0
    assert 0 < shift <= wl.W_CAP * wl.SCALE * (wl.LIFT_CLAMP - wl.DEAD_BAND) + 1e-9
    # w grows with evidence: n / (n + K).
    few = wl.model({**{wl.tool_key(t[0]): row(40) for t in TOOLS}, wl.tool_key("Adobe Firefly"): row(40, tries=10)}, section())
    assert 0 < few["cards"][wl.tool_key("Adobe Firefly")]["w"] < wl.W_CAP


def test_the_hard_rules_still_win():
    stories = section()
    dev = work.clean_card(card(tool="Zapier Agents", maker="Zapier", effort="needs a developer",
                               link="https://zapier.com/agents"))
    s = with_card(7, {7: dev})
    s["articles"] = [{"id": 7, "domain": "theverge.com"}]
    handle = work.clean_card(card(tool="Prompt Pack", maker="MoistTonight3997", link="https://promptpack.io"))
    h = with_card(8, {8: handle})
    h["articles"] = [{"id": 8, "domain": "theverge.com"}]
    stories += [s, h]
    # Readers love both: every view a try.
    by_tool = {wl.tool_key(t[0]): row(300, tries=6) for t in TOOLS}
    by_tool[wl.tool_key("Zapier Agents")] = row(300, tries=300)
    by_tool[wl.tool_key("Prompt Pack")] = row(300, tries=300)
    learned = wl.model(by_tool, stories)
    wl.apply(stories, learned)
    assert s["workCard"]["usefulness"] > s["workCard"]["rulesUsefulness"]
    brief = work.build_briefing(stories, NOW)
    assert brief["featuredId"] not in (7, 8), "never a developer card or a forum handle as the featured pick"
    assert brief["storyIds"][0] == brief["featuredId"]
    assert work.featurable(next(x for x in stories if x["id"] == brief["featuredId"]))
    # The rules' own fields are untouched: the skip reason still says why to leave it.
    assert s["workCard"]["skip"] == "needs a developer"


def test_recent_events_count_more_than_old_ones():
    # The read's weights: a day-old event counts almost fully, a four-week-old one about a quarter.
    whens = wl._weight_expr(db.events.c.created_at, NOW).whens
    first, last = whens[0][1].value, whens[-1][1].value
    assert abs(first - 0.5 ** (0.5 / 14)) < 1e-3 and abs(last - 0.5 ** (29.5 / 14)) < 1e-3
    # Same raw counts; the tries of one card are recent, the other's four weeks old.
    by_tool = {wl.tool_key(t[0]): row(300, tries=6) for t in TOOLS}
    recent, old = wl.tool_key("Canva Magic Studio"), wl.tool_key("Adobe Firefly")
    by_tool[recent] = {**row(300), "try": 60, "wtry": 60 * 0.97, "wviews": 300 * 0.6}
    by_tool[old] = {**row(300), "try": 60, "wtry": 60 * 0.25, "wviews": 300 * 0.6}
    learned = wl.model(by_tool, section())
    assert learned["cards"][recent]["lift"] > learned["cards"][old]["lift"] > 0


# ---------------------------------------------------------------------------- the read and its cache

def _seed(eng, now):
    from sqlalchemy import insert

    base = {"story_id": None, "value": 1, "path": "/work"}
    rows = []
    for i in range(10):
        rows.append({**base, "type": "card_view", "detail": "Canva Magic Studio", "created_at": now - timedelta(hours=i)})
    for i in range(4):
        rows.append({**base, "type": "card_view", "detail": "Adobe Firefly", "created_at": now - timedelta(days=20)})
    rows += [
        {**base, "type": "try", "detail": "Canva Magic Studio", "created_at": now},
        {**base, "type": "try", "detail": "Canva Magic Studio", "created_at": now - timedelta(days=2)},
        {**base, "type": "copy_prompt", "detail": "Canva Magic Studio", "created_at": now},
        {**base, "type": "expand", "detail": "Adobe Firefly", "created_at": now - timedelta(days=20)},
        {**base, "type": "try", "detail": "Canva Magic Studio", "created_at": now - timedelta(days=40)},  # outside the window
        {**base, "type": "next_click", "detail": "home-teaser", "created_at": now},                       # not a card action
        {**base, "type": "view", "detail": None, "created_at": now},
        {**base, "type": "card_view", "detail": None, "created_at": now},                                  # no tool
    ]
    with eng.begin() as conn:
        conn.execute(insert(db.events), [{"session": f"s{i}", "article_id": None, "visitor": None, "source": None, "tz": None, **r}
                                         for i, r in enumerate(rows)])


def test_the_aggregate_is_one_grouped_row_per_tool():
    import test_reads as tr

    now = datetime.now(timezone.utc)
    with tr.fresh_db() as (eng, _tmp):
        _seed(eng, now)
        with eng.connect() as conn:
            rows = wl.read_counts(conn, now)
    by = {r[0]: r for r in rows}
    assert set(by) == {"Canva Magic Studio", "Adobe Firefly"}, rows
    assert rows[0][0] == "Canva Magic Studio", "most viewed first"
    canva, adobe = by["Canva Magic Studio"], by["Adobe Firefly"]
    assert canva[1:5] == (10, 2, 1, 0) and adobe[1:5] == (4, 0, 0, 1)
    # Recency: Canva's views are hours old (weight ~0.98 each), Adobe's twenty days old (~0.36).
    assert 9.5 < canva[5] <= 10 and 1.2 < adobe[5] < 1.6, (canva, adobe)


def test_the_cache_avoids_reading_again_for_a_few_hours():
    import test_reads as tr
    from digest import cache

    now = datetime.now(timezone.utc)
    with tr.fresh_db() as (eng, _tmp):
        _seed(eng, now)
        with eng.connect() as conn:
            first, info1 = wl.counts(conn, now)
            b0 = db.bytes_read()
            second, info2 = wl.counts(conn, now)
            assert db.bytes_read() == b0, "the second call reads nothing"
            assert not info1["fromCache"] and info1["readKB"] >= 0 and info2["fromCache"] and info2["readKB"] == 0
            assert first == second and first[wl.tool_key("Canva Magic Studio")]["try"] == 2
            # Saved to disk and loaded again by the next run.
            cache.save_all()
            cache.reset()
            b0 = db.bytes_read()
            third, info3 = wl.counts(conn, now)
            assert info3["fromCache"] and db.bytes_read() == b0 and third == first
            # Older than the refresh interval: read once more.
            st = cache.WORK_EVENTS.get(conn)
            st["at"] = time.time() - (wl.REFRESH_HOURS + 1) * 3600
            _fourth, info4 = wl.counts(conn, now)
            assert not info4["fromCache"] and db.bytes_read() > b0


def test_the_export_blends_reads_once_and_says_what_readers_taught():
    import json

    import test_reads as tr
    from sqlalchemy import update

    from digest import config, export

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng, stories=6, per_story=2)
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id == 1).values(work_card=work.clean_card(card())))
        config.SITE_DATA_DIR = tmp / "site"
        stats = export.run()
        brief = json.loads((config.SITE_DATA_DIR / "work-briefing.json").read_text(encoding="utf-8"))
        again = export.run()
    assert stats["workLearn"]["active"] is False and stats["workLearn"]["fromCache"] is False
    assert again["workLearn"]["fromCache"] is True and again["workLearn"]["readKB"] == 0
    assert brief["learning"]["text"].startswith("Not enough reader data yet (0 card views; learning starts to count from about 200)")
    assert brief["learning"]["changed"] is None


# ---------------------------------------------------------------------------- what the admin page and the note say

def test_the_admin_sentences_and_the_morning_clause():
    stories = section()
    rules = [s["id"] for s in sorted(stories, key=lambda s: -s["workCard"]["usefulness"])]
    by_tool = {wl.tool_key(t[0]): row(150, tries=3) for t in TOOLS}
    by_tool[wl.tool_key("Notion AI")] = row(400, tries=60, copies=10)
    learned = wl.model(by_tool, stories)
    wl.apply(stories, learned)
    blended = [s["id"] for s in sorted(stories, key=lambda s: -s["workCard"]["usefulness"])]
    out = wl.taught(learned, stories, rules, blended)
    assert out["active"] and 0 < out["dataShare"] <= 60
    assert out["sentences"] and "as often as the average card" in out["sentences"][0], out
    assert any(m["tool"] == "Notion AI" and m["to"] < m["from"] for m in out["movers"])
    assert "Notion" in " ".join(out["sentences"]), out["sentences"]
    empty = wl.taught(wl.model({}, stories), stories, rules, rules)
    assert empty["text"] == "Not enough reader data yet (0 card views; learning starts to count from about 200)."

    base = "Nothing moved."
    assert morning.with_work(base, None) == base
    feat = morning.with_work(base, {"kind": "featured", "to": "Plan posts faster", "from": "Resize ads"})
    assert feat == "Nothing moved; on AI at Work, reader data made “Plan posts faster” the featured pick instead of “Resize ads”."
    top = morning.with_work(base, {"kind": "top3", "to": "Plan posts faster", "at": 2, "was": 5})
    assert top.endswith("put “Plan posts faster” at #2 (the rules alone had it at #5).")
    assert top.count(".") == 1, "still one sentence"


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
