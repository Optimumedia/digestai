"""The morning note: python tests/test_morning.py (offline; SQLite only, no model, no GitHub)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import config, morning  # noqa: E402

NOW = datetime(2026, 9, 19, 5, 7, tzinfo=timezone.utc)


def _story(sid, headline, score, domains, primary=False, category="models", first="2026-09-18T20:00:00Z", pinned=False):
    return {"id": sid, "slug": f"s{sid}", "headline": headline, "score": score, "category": category, "pinned": pinned,
            "hasPrimary": primary, "firstPublishedAt": first,
            "articles": [{"domain": d, "source": "OpenAI" if primary and i == 0 else d, "sourceType": "primary" if primary and i == 0 else "press"}
                         for i, d in enumerate(domains)]}


STORIES = [
    _story(1, "Mistral raises $600M Series C", 0.812, ["techcrunch.com"]),
    _story(2, "OpenAI ships GPT-6 to all API users", 0.774, ["openai.com", "theverge.com", "news.theverge.com", "wired.com"], primary=True),
    _story(3, "Nvidia opens a Warsaw research lab", 0.702, ["reuters.com", "ft.com"], category="hardware"),
    _story(4, "Small shops use AI receptionists", 0.655, ["forbes.com"], category="marketing"),
    _story(5, "EU publishes AI Act guidance", 0.61, ["europa.eu"], primary=True, category="policy"),
    _story(6, "A study of 4,000 prompts", 0.52, ["arxiv.org"], category="research"),
    _story(7, "Robot arms learn from video", 0.41, ["wired.com"], category="robotics"),
]
BRIEFING = {"storyIds": [2, 1, 3, 4, 5]}
ADMIN = {
    "sources": [
        {"key": "openai", "name": "OpenAI", "enabled": True, "discovered": False, "performance": 1.62, "errorCount": 0},
        {"key": "verge", "name": "The Verge", "enabled": True, "discovered": False, "performance": 0.91, "errorCount": 0},
        {"key": "tc", "name": "TechCrunch", "enabled": True, "discovered": False, "performance": 0.58, "errorCount": 0},
        {"key": "deadfeed", "name": "VentureBeat", "enabled": True, "discovered": False, "performance": 0.3, "errorCount": 3 * config.RUNS_PER_DAY + 8},
        {"key": "search-x", "name": "Search: x", "enabled": True, "discovered": True, "performance": 2.0, "errorCount": 99},
    ],
    "llm": {"budgets": {"gemini": 54, "groq": 2400},
            "usage": [{"day": "2026-09-18", "provider": "gemini", "requests": 54, "exhausted": True},
                      {"day": "2026-09-18", "provider": "groq", "requests": 310, "exhausted": False}]},
    "database": {"reads": {"monthlyMB": 702.3, "quotaMB": 5120.0}, "cycle": {"projectedMB": 3900.2, "measuredMB": 1210.4, "end": "2026-10-11"}},
    "runs": [{"step": "fetch", "startedAt": "2026-09-18T12:07:00Z", "stats": {"readKB": 700.0}},
             {"step": "admin", "startedAt": "2026-09-18T12:09:00Z", "stats": {"readKB": 124.0}},
             {"step": "fetch", "startedAt": "2026-09-19T04:07:00Z", "stats": {"readKB": 200.0}},
             {"step": "fetch", "startedAt": "2026-09-17T04:07:00Z", "stats": {"readKB": 9999.0}}],
    "engagement": {"perDay": [{"day": "2026-09-17", "visitors": 30}, {"day": "2026-09-18", "visitors": 42}],
                   "searches7": {"missing": []}},
}
# Reader events of the last 24 hours, per story, as reader_events() returns them.
EVENTS = {
    1: {"views": 4, "dwell": 120.0, "dwellN": 4, "clicks": 1, "depth": 55, "depthN": 4},
    2: {"views": 9, "dwell": 400.0, "dwellN": 8, "clicks": 3, "depth": 71, "depthN": 8},
    6: {"views": 14, "dwell": 70.0, "dwellN": 10, "clicks": 0, "depth": 18, "depthN": 12},
}
PAIRS = ([("direct", "Europe/London", f"v{i}") for i in range(12)]
         + [("google.com", "America/New_York", f"g{i}") for i in range(8)]
         + [("bsky.app", "Europe/Warsaw", f"b{i}") for i in range(5)]
         + [("www.bing.com", "America/Chicago", "g0")])  # the same visitor twice: counted once in search
PREV = {"day": "2026-09-18", "weights": {"openai": 1.21, "verge": 0.95, "tc": 0.6}}


def _rules_note():
    os.environ["MORNING_NOTE_POLISH"] = "0"
    try:
        return morning.build(ADMIN, BRIEFING, STORIES, EVENTS, PAIRS, PREV, NOW)
    finally:
        os.environ.pop("MORNING_NOTE_POLISH", None)


def test_rules_only_note_from_fixture():
    note = _rules_note()
    text = [s["text"] for s in note["sentences"]]
    assert note["title"] == "Morning note, 19 Sep 2026"
    assert [s["key"] for s in note["sentences"]] == ["briefing", "readers", "sources", "budget", "growth", "decide"]
    assert text[0] == ("“OpenAI ships GPT-6 to all API users” led the briefing because confirmed stories go first: it moved "
                       "above “Mistral raises $600M Series C” (score 0.812), which only one publisher had reported; it scored "
                       "0.774, with 3 publishers and the primary source (OpenAI)."), text[0]
    assert text[1] == ("Readers opened “A study of 4,000 prompts” most (14 views, 18% average read depth), the biggest "
                       "surprise, as the ranking had it at #6 while its first choice, “Mistral raises $600M Series C”, "
                       "drew 4 views."), text[1]
    assert text[2] == ("OpenAI earned the most weight since the note of 18 Sep: its learned score rose from 1.21 to 1.62, "
                       "where 1.00 is an average source."), text[2]
    assert text[3] == ("Gemini used all of its free allowance yesterday (54 of 54 requests), so later summaries went to the "
                       "other models."), text[3]
    assert text[4] == ("Visitors rose to 42 on 18 Sep from 30 the day before; 12 came direct, 8 from search and 5 from "
                       "Bluesky; most were in Britain (12) and the United States (8)."), text[4]
    assert text[5] == ("Should VentureBeat be paused or given a new feed address? It has failed on each of its last "
                       f"{3 * config.RUNS_PER_DAY + 8} runs, 3 days or more."), text[5]
    assert note["polish"] == {"polished": False, "reason": "off"}
    assert note["rules"] == text


def test_decide_order_and_fallbacks():
    admin = json.loads(json.dumps(ADMIN))
    admin["sources"] = [s for s in admin["sources"] if s["key"] != "deadfeed"]
    note = morning.build(admin, BRIEFING, STORIES, EVENTS, PAIRS, PREV, NOW, call=lambda p: {})
    assert note["sentences"][5]["text"] == ("Should the summary of “A study of 4,000 prompts” be rewritten? It drew 14 "
                                            "views, but readers got on average only 18% of the way down the page.")
    # Over the egress allowance: that is both the budget fact and, with no failing feed, the question.
    admin["database"]["cycle"]["projectedMB"] = 6200.0
    note = morning.build(admin, BRIEFING, STORIES, EVENTS, PAIRS, PREV, NOW, call=lambda p: {})
    assert note["sentences"][3]["text"].startswith("Database reads are on course for 6,200.0 MB this billing cycle, over the 5,120 MB")
    assert note["sentences"][5]["text"].startswith("Should the pipeline run less often")
    # Nothing to report at all: empty data still gives six plain sentences.
    empty = morning.build({}, None, [], {}, [], None, NOW, call=lambda p: {})
    assert len(empty["sentences"]) == 6
    assert empty["sentences"][0]["text"].startswith("The briefing had no lead story")
    assert empty["sentences"][1]["text"].startswith("No reader opened")
    assert empty["sentences"][5]["text"] == "Nothing is far enough off to need a decision today."


def test_gate_once_a_day_from_five():
    assert not morning.due(datetime(2026, 9, 19, 4, 59, tzinfo=timezone.utc), [])
    assert morning.due(datetime(2026, 9, 19, 5, 0, tzinfo=timezone.utc), [])
    assert morning.due(NOW, [{"day": "2026-09-18"}])
    assert not morning.due(NOW, [{"day": "2026-09-19"}])
    assert not morning.due(datetime(2026, 9, 19, 23, 7, tzinfo=timezone.utc), [{"day": "2026-09-19"}])
    # A day whose 05:00 run failed: the 06:00 run writes it.
    assert morning.due(datetime(2026, 9, 19, 6, 7, tzinfo=timezone.utc), [{"day": "2026-09-18"}])


def test_polish_kept_only_when_facts_survive():
    rules = _rules_note()["rules"]
    good = list(rules)
    good[3] = "Yesterday Gemini used all of its free allowance (54 of 54 requests), so the other models wrote later summaries."
    text, info = morning.polish(rules, call=lambda p: {"sentences": good})
    assert info["polished"] and text == good
    # An invented figure: the rules text stays.
    bad = list(rules)
    bad[4] = bad[4].replace("rose to 42", "rose to 45")
    text, info = morning.polish(rules, call=lambda p: {"sentences": bad})
    assert text == rules and not info["polished"] and "figures changed" in info["reason"], info
    # A dropped figure, a rounded one, an invented name, the wrong shape, a failing model.
    dropped = list(rules)
    dropped[3] = "Gemini used all of its free allowance yesterday, so later summaries went to the other models."
    assert morning.polish(rules, call=lambda p: {"sentences": dropped})[1]["reason"].startswith("sentence 4: figures")
    rounded = list(rules)
    rounded[2] = rounded[2].replace("1.62", "1.6")
    assert not morning.polish(rules, call=lambda p: {"sentences": rounded})[1]["polished"]
    named = list(rules)
    named[3] = named[3].replace("the other models", "Anthropic's models")
    assert "names" in morning.polish(rules, call=lambda p: {"sentences": named})[1]["reason"]
    assert morning.polish(rules, call=lambda p: {"sentences": rules[:5]})[1]["reason"] == "not six sentences"

    def boom(prompt):
        raise RuntimeError("quota")

    text, info = morning.polish(rules, call=boom)
    assert text == rules and info["reason"].startswith("model failed")


def test_events_and_visitors_are_grouped_in_sql():
    from sqlalchemy import create_engine, insert

    from digest import db

    tmp = Path(tempfile.mkdtemp()) / "morning.db"
    eng = create_engine(f"sqlite:///{tmp.as_posix()}", future=True)
    db.metadata.create_all(eng)
    at = NOW - timedelta(hours=3)
    rows = [
        ("view", "a", "va", 1, 1, "direct", "Europe/London"), ("view", "b", "vb", 1, 1, "google.com", "Europe/Warsaw"),
        ("dwell", "a", "va", 1, 30, None, None), ("click_source", "a", "va", 1, 1, None, None),
        ("depth", "a", "va", 1, 40, None, None), ("depth", "a", "va", 1, 90, None, None),  # one session, deepest 90
        ("depth", "b", "vb", 1, 30, None, None),
    ]
    with eng.begin() as conn:
        conn.execute(insert(db.events), [{"type": t, "session": s, "visitor": v, "story_id": sid, "value": val,
                                          "source": src, "tz": tz, "detail": "top" if t == "depth" else None,
                                          "created_at": at} for t, s, v, sid, val, src, tz in rows])
        conn.execute(insert(db.events), [{"type": "view", "session": "old", "story_id": 1, "value": 1, "created_at": NOW - timedelta(days=3)}])
    with eng.connect() as conn:
        ev = morning.reader_events(conn, NOW - timedelta(hours=24))
        pairs = morning.visitor_pairs(conn, NOW - timedelta(hours=24), NOW)
    assert ev[1]["views"] == 2 and ev[1]["clicks"] == 1 and ev[1]["dwellN"] == 1
    assert ev[1]["depth"] == 60 and ev[1]["depthN"] == 2  # (90 + 30) / 2
    assert sorted(pairs) == [("direct", "Europe/London", "va"), ("google.com", "Europe/Warsaw", "vb")]


def test_store_export_and_issue_embedding():
    old = (config.CACHE_DIR, config.SITE_DATA_DIR)
    tmp = Path(tempfile.mkdtemp())
    config.CACHE_DIR, config.SITE_DATA_DIR = tmp / "cache", tmp / "data"
    try:
        note = _rules_note()
        older = [dict(note, day=f"2026-09-{d:02d}", title=morning.title_for(f"2026-09-{d:02d}")) for d in range(18, 2, -1)]
        morning.save_notes([note] + older)
        stats = morning.run(now=NOW)  # today's note exists: no database, no model, only the export
        assert stats["written"] is False and stats["reason"] == "already written today" and stats["kept"] == 14
        public = json.loads((config.SITE_DATA_DIR / "morning.json").read_text(encoding="utf-8"))["notes"]
        assert len(public) == 14 and public[0]["day"] == "2026-09-19" and "weights" not in public[0]
        back = morning.unembed("text\n" + morning.embed(note))
        assert back["day"] == "2026-09-19" and back["weights"]["openai"] == 1.62 and back["sentences"] == note["sentences"]
        from digest import notify

        body = notify.note_body(note)
        assert "**To decide.** Should VentureBeat" in body and morning.unembed(body)["title"] == note["title"]
    finally:
        config.CACHE_DIR, config.SITE_DATA_DIR = old


def test_budget_and_reader_wording_for_early_limits_and_zero_views():
    b = {"quotaMB": 5120, "projectedMB": None, "readMB": None, "exhausted": [{"provider": "gemini", "requests": 26, "budget": 54}]}
    assert morning.s_budget(b) == ("Gemini hit its provider's limit yesterday after 26 requests of the 54 the pipeline allows it, "
                                   "so later summaries went to the other models."), morning.s_budget(b)
    b["exhausted"][0]["requests"] = 54
    assert morning.s_budget(b).startswith("Gemini used all of its free allowance yesterday (54 of 54 requests)")
    top = {"headline": "A", "views": 6, "rank": 2, "depth": 12, "clicks": 0, "dwell": None}
    r = {"views": 6, "top": [top], "first": {"headline": "B", "views": 0, "rank": 1}, "rows": [top]}
    text = morning.s_readers(r)
    assert "drew no views" in text and "0 views" not in text, text


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
