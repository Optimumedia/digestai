"""The AI team (digest/team.py): six roles over data the run already has."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import team  # noqa: E402

NOW = datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)


def _admin(readers=(30,) * 7 + (40,) * 7, crashed=False, run_seconds=300):
    days = [(NOW - timedelta(days=len(readers) - i)).date().isoformat() for i in range(len(readers))]
    per_day = [{"day": d, "visitors": v} for d, v in zip(days, readers)] + [{"day": NOW.date().isoformat(), "visitors": 3}]
    start = (NOW - timedelta(minutes=20)).isoformat()
    return {
        "runs": [{"step": "fetch", "startedAt": start, "stats": {"seconds": run_seconds / 2}},
                 {"step": "export", "startedAt": start, "stats": {"seconds": run_seconds / 2, **({"crashed": True} if crashed else {})}}],
        "engagement": {"perDay": per_day, "automated7": {"visitors": 2400},
                       "sources7": [{"name": "Direct", "visitors": 200}, {"name": "ChatGPT", "visitors": 41}, {"name": "LinkedIn", "visitors": 9}],
                       "searches7": {"top": [{"query": "gpt-6", "results": 4}], "missing": [{"query": "sora pricing", "results": 0}]},
                       "pages7": [{"path": "/"}, {"path": "/today"}]},
        "sources": [{"key": "a", "name": "MarTech", "enabled": True, "errorCount": 9},
                    {"key": "b", "name": "OK feed", "enabled": True, "errorCount": 0}],
        "llm": {"usage": [{"day": NOW.date().isoformat(), "provider": "groq", "exhausted": True},
                          {"day": NOW.date().isoformat(), "provider": "openrouter", "exhausted": True}]},
        "database": {"sizeMB": 190, "limitMB": 500, "reads": {"quotaMB": 5120}, "cycle": {"projectedMB": 900}},
        "actions": [{"level": "warning", "what": "x"}],
    }


def test_the_guard_sees_a_site_that_stopped_and_the_team_puts_it_first():
    t = team.build(_admin(crashed=True), None, None, NOW, site_hours=390.0)
    g = t["roles"]["guard"]
    assert g["status"] == "act" and "390.0 hours" in g["headline"], g
    assert any(f["key"] == "crashed:export" for f in g["findings"])
    assert any(f["key"] == "bot-wave" and "2,400 to 280" in f["text"] for f in g["findings"]), g["findings"]
    plan = t["roles"]["brain"]["plan"]
    assert plan[0]["role"] == "guard" and plan[0]["owner"] == "Claude" and plan[0]["level"] == "act", plan
    # Readers: 40 a day this week against 30 the week before (today, still running, left out).
    goal = t["roles"]["brain"]["goal"]
    assert goal == {"readersPerDay": 40.0, "days": 7, "previous": 30.0, "changePct": 33}, goal
    assert t["roles"]["brain"]["brief"][0] == "Readers: 40 a day this week, up 33% on last week (30)."
    # Every role is there, with the arrows between them.
    assert list(t["roles"]) == team.ORDER and {h["from"] for h in t["handoffs"]} >= {"guard", "market", "finisher", "brain"}


def test_platform_and_market_say_what_they_see():
    t = team.build(_admin(run_seconds=1150), {"start": "2026-10-26", "end": "2026-11-21",
                                              "queries": [{"query": "ai news today", "impressions": 80, "clicks": 0}],
                                              "perDay": [{"clicks": 2}, {"clicks": 1}]}, None, NOW, site_hours=0.4)
    p = {f["key"]: f for f in t["roles"]["platform"]["findings"]}
    assert p["run-time"]["level"] == "act" and "19 of its 20 minutes" in p["run-time"]["text"], p
    assert "MarTech" in p["sources-failing"]["text"] and "OK feed" not in p["sources-failing"]["text"]
    assert p["models-spent"]["level"] == "watch" and "db-size" not in p and "egress" not in p
    m = {f["key"]: f for f in t["roles"]["market"]["findings"]}
    assert m["referrers"]["text"] == "Readers this week came from ChatGPT (41), LinkedIn (9)."
    assert '"sora pricing"' in m["search-missing"]["text"] and m["search-missing"]["level"] == "watch"
    assert '"ai news today"' in m["search-shown"]["text"] and m["search-clicks"]["text"].startswith("Google sent 3 clicks")
    assert not any(f["key"] == "site-stale" for f in t["roles"]["guard"]["findings"])


def test_the_finisher_keeps_items_open_until_they_are_gone_and_ages_them():
    owner = [{"key": "token", "text": "Make a new token.", "owner": "Martin", "level": "act", "done": False}]
    t1 = team.build(_admin(crashed=True), None, None, NOW, site_hours=390.0, owner_items=owner)
    led = t1["ledger"]
    assert "guard:site-stale" in led and "owner:token" in led and not led["guard:site-stale"]["closedAt"]
    # Eight days later the site is still stale: the item is the same one, now overdue.
    later = NOW + timedelta(days=8)
    t2 = team.build(_admin(crashed=True), None, t1, later, site_hours=580.0, owner_items=owner)
    fin = t2["roles"]["finisher"]
    assert t2["ledger"]["guard:site-stale"]["openedAt"] == led["guard:site-stale"]["openedAt"]
    assert fin["overdue"] >= 2 and fin["status"] == "act", fin
    assert any("Open 8 days (Martin): Make a new token." == f["text"] for f in fin["findings"]), fin["findings"]
    # The site is fixed and the token marked done: both close; info findings never enter the ledger.
    owner[0]["done"] = True
    t3 = team.build(_admin(), None, t2, later + timedelta(hours=1), site_hours=0.2, owner_items=owner)
    assert t3["ledger"]["guard:site-stale"]["closedAt"] and t3["ledger"]["owner:token"]["closedAt"]
    assert not any(k.startswith("market:referrers") for k in t3["ledger"])
    assert t3["roles"]["finisher"]["closed7"] >= 2


def test_roles_keep_their_rhythm_and_a_closed_item_needs_its_role_to_run_again():
    t1 = team.build(_admin(), None, None, NOW, site_hours=0.1)
    assert set(t1["ranNow"]) == set(team.ORDER)
    t2 = team.build(_admin(), None, t1, NOW + timedelta(minutes=20), site_hours=0.1)
    assert "market" not in t2["ranNow"] and "ideas" not in t2["ranNow"] and "guard" in t2["ranNow"]
    assert t2["roles"]["market"]["ranAt"] == t1["roles"]["market"]["ranAt"]
    # Market's "found nothing" search stays open while market has not run again.
    assert not t2["ledger"]["market:search-missing"]["closedAt"]


def test_ideas_come_from_a_free_model_and_never_bring_numbers_nobody_measured():
    asked = []

    def model(prompt):
        asked.append(prompt)
        return {"ideas": [
            {"title": "Answer the questions readers search for", "why": "Readers searched for sora pricing and found nothing.", "firstStep": "Add a pricing answer."},
            {"title": "Promise 10x traffic", "why": "It will bring 50,000 readers.", "firstStep": "Post it."},
            {"title": "Weekly ChatGPT digest page", "why": "ChatGPT sends the most readers.", "firstStep": "Write the page."},
        ]}

    t = team.build(_admin(), None, None, NOW, site_hours=0.1, call=model)
    titles = [i["title"] for i in t["roles"]["ideas"]["ideas"]]
    assert titles == ["Answer the questions readers search for", "Weekly ChatGPT digest page"], titles
    assert "ChatGPT (41)" in asked[0] and "none" in asked[0]
    # Next week the earlier titles are passed as "do not repeat"; with no model, last week's stand.
    nxt = team.build(_admin(), None, t, NOW + timedelta(days=7, hours=1), site_hours=0.1, call=None)
    assert nxt["roles"]["ideas"]["ideas"] == t["roles"]["ideas"]["ideas"] and "no free model" in nxt["roles"]["ideas"]["headline"]
    t2 = team.build(_admin(), None, t, NOW + timedelta(days=7, hours=1), site_hours=0.1, call=model)
    assert "Answer the questions readers search for" in asked[-1].split("earlier ideas:")[1]
    # The model only repeated itself: nothing new passes, and last week's ideas stand, saying why.
    assert t2["roles"]["ideas"]["ideas"] == t["roles"]["ideas"]["ideas"]
    assert "did not pass the checks" in t2["roles"]["ideas"]["headline"], t2["roles"]["ideas"]["headline"]
    # The plan tries an idea when fewer than three items are open.
    calm = team.build({"engagement": {"perDay": []}}, None, None, NOW, site_hours=0.1, call=model)
    assert any(p["role"] == "ideas" for p in calm["roles"]["brain"]["plan"]), calm["roles"]["brain"]["plan"]


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - report every test, not just the first error
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
