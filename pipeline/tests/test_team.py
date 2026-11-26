"""The AI team (digest/team.py): six roles over data the run already has."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import team  # noqa: E402

NOW = datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)


def _day(back: int) -> str:
    return (NOW - timedelta(days=back)).date().isoformat()


def _admin(readers=(30,) * 7 + (40,) * 7, crashed=False, run_seconds=300):
    days = [(NOW - timedelta(days=len(readers) - i)).date().isoformat() for i in range(len(readers))]
    per_day = [{"day": d, "visitors": v} for d, v in zip(days, readers)] + [{"day": NOW.date().isoformat(), "visitors": 3}]
    start = (NOW - timedelta(minutes=20)).isoformat()
    return {
        "runs": [{"step": "fetch", "startedAt": start, "stats": {"seconds": run_seconds / 2}},
                 {"step": "export", "startedAt": start, "stats": {"seconds": run_seconds / 2, **({"crashed": True} if crashed else {})}}],
        "engagement": {"perDay": per_day, "automated7": {"visitors": 2400},
                       "sources7": [{"name": "Direct", "visitors": 200}, {"name": "ChatGPT", "visitors": 41}, {"name": "LinkedIn", "visitors": 9}],
                       "searches7": {"top": [{"query": "gpt-6", "results": 4, "visitors": 3}],
                                     "missing": [{"query": "sora pricing", "results": 0, "visitors": 3},
                                                 {"query": "stacy fan", "results": 0, "visitors": 1}]},
                       "pages7": [{"path": "/", "visitors": 90}, {"path": "/today", "visitors": 12}]},
        "sources": [{"key": "a", "name": "MarTech", "enabled": True, "errorCount": 9},
                    {"key": "b", "name": "OK feed", "enabled": True, "errorCount": 0}],
        "llm": {"usage": [{"day": NOW.date().isoformat(), "provider": "groq", "exhausted": True},
                          {"day": NOW.date().isoformat(), "provider": "openrouter", "exhausted": True}]},
        "database": {"sizeMB": 190, "limitMB": 500, "reads": {"quotaMB": 5120}, "cycle": {"projectedMB": 900}},
        "schedule": {"runsPerDay": 72},
        "actions": [{"id": "quality:headline_check", "level": "warning", "what": "3 headlines failed the check at {at}.",
                     "at": "2026-11-23T09:47:00Z", "todo": "Open the Content tab and fix them.", "why": "Readers see them."},
                    {"id": "source:a", "level": "warning", "what": "MarTech has failed to load 12 runs in a row."}],
    }


def _run(start: datetime, steps: dict[str, float], crashed: tuple = ()) -> list[dict]:
    """One pipeline run's step rows, a step 20 seconds apart."""
    return [{"step": s, "startedAt": (start + timedelta(seconds=20 * i)).isoformat(),
             "stats": {"seconds": sec, **({"crashed": True} if s in crashed else {})}} for i, (s, sec) in enumerate(steps.items())]




def test_the_guard_sees_a_site_that_stopped_and_the_team_puts_it_first():
    t = team.build(_admin(crashed=True), None, None, NOW, site_hours=390.0)
    g = t["roles"]["guard"]
    assert g["status"] == "act" and "390.0 hours" in g["headline"], g
    keys = {f["key"]: f for f in g["findings"]}
    assert keys["crashed"]["impact"] == "outage" and keys["site-stale"]["fix"].startswith("Open the latest run"), keys
    assert "2,400 to 280" in keys["bot-wave"]["text"] and keys["bot-wave"]["level"] == "info", keys["bot-wave"]
    # The Today tab's cards are named, with their own fix; the ones another role reports are not repeated.
    card = keys["card:quality:headline_check"]
    assert card["text"] == "3 headlines failed the check at 09:47 UTC." and card["fix"] == "Open the Content tab and fix them.", card
    assert "card:source:a" not in keys
    plan = t["roles"]["brain"]["plan"]
    assert plan[0]["role"] == "guard" and plan[0]["owner"] == "Claude" and plan[0]["impact"] == "outage" and plan[0]["fix"], plan
    # Readers: 40 a day this week against 30 the week before (today, still running, left out).
    goal = t["roles"]["brain"]["goal"]
    assert {k: goal[k] for k in ("readersPerDay", "days", "previous", "changePct")} == \
        {"readersPerDay": 40.0, "days": 7, "previous": 30.0, "changePct": 33}, goal
    assert len(goal["last"]) == 14 and goal["last"][-1]["readers"] == 40
    assert t["roles"]["brain"]["brief"][0] == "Readers: 40 a day over the last 7 complete days, up 33% on the week before (30)."
    # Every role is there, with the arrows between them.
    assert list(t["roles"]) == team.ORDER and {h["from"] for h in t["handoffs"]} >= {"guard", "market", "finisher", "brain"}


def test_readers_come_from_the_daily_history_readers_only_and_complete_days():
    """The engagement block holds 14 days including today, so a week against the week before never
    had its 14 complete days; the history does. Automated visits and days the counter could not
    record are left out."""
    hist = [{"day": _day(i), "visitors": 50, "automatedVisits": 30} for i in range(15, 7, -1)]  # 20 readers
    hist += [{"day": _day(i), "visitors": 60, "automatedVisits": 35} for i in range(7, 0, -1)]  # 25 readers
    hist += [{"day": _day(0), "visitors": 2, "automatedVisits": 0}]  # today, still running
    hist[0] = {"day": _day(15), "visitors": None, "noData": "blocked"}
    admin = {"history": hist, "engagement": {"perDay": [{"day": _day(1), "visitors": 999}]}}
    days = team.readers_per_day(admin, NOW.date().isoformat())
    assert days[-1] == (_day(1), 25) and len(days) == 14 and _day(15) not in dict(days), days
    goal = team.build(admin, None, None, NOW, site_hours=0.2)["roles"]["brain"]["goal"]
    assert goal["readersPerDay"] == 25.0 and goal["previous"] == 20.0 and goal["changePct"] == 25, goal


def test_platform_and_market_say_what_they_see():
    t = team.build(_admin(run_seconds=1150), {"start": "2026-10-26", "end": "2026-11-21",
                                              "queries": [{"query": "ai news today", "impressions": 80, "clicks": 0, "position": 12.4}],
                                              "perDay": [{"clicks": 1}]}, None, NOW, site_hours=0.4)
    p = {f["key"]: f for f in t["roles"]["platform"]["findings"]}
    assert p["run-time"]["level"] == "act" and "19.2 of its 18 budgeted minutes" in p["run-time"]["text"], p
    assert "MarTech has failed 9 runs in a row (a few hours)" in p["source:a"]["text"] and p["source:a"]["level"] == "info", p
    assert "source:b" not in p
    assert p["models-spent"]["level"] == "info" and "db-size" not in p and "egress-cycle" not in p
    m = {f["key"]: f for f in t["roles"]["market"]["findings"]}
    assert m["referrers"]["text"] == "Readers this week came from ChatGPT 41, LinkedIn 9."
    # A search one person made is not a to-do; one several readers made is, with the number.
    assert m["search-missing"]["text"] == 'Readers searched the site and found nothing for: "sora pricing" (3 readers).', m["search-missing"]
    assert m["search-missing"]["level"] == "watch" and m["search-missing"]["fix"]
    assert m["search-shown"]["text"] == 'Google showed the site without a click for "ai news today" (80 times, position 12).', m["search-shown"]
    assert m["search-clicks"]["text"] == "Google sent 1 click from 26 Oct to 21 Nov."
    assert m["pages-read"]["text"] == "Most read pages: /today (12 readers)."
    assert not any(f["key"] == "site-stale" for f in t["roles"]["guard"]["findings"])


def test_platform_notices_skipped_steps_cut_runs_failing_feeds_and_egress():
    from digest.run import ORDER

    full = {s: 30.0 for s in ORDER[:ORDER.index("admin") + 1]}
    full["enrich"] = 480.0
    short = {s: v for s, v in full.items() if s not in ("topics", "images")}  # skipped for time
    runs = []
    for i in range(6):
        runs += _run(NOW - timedelta(minutes=20 * (8 - i)), short if i % 3 else full)
    runs += _run(NOW - timedelta(minutes=40), {"fetch": 30, "extract": 30, "enrich": 900})  # cut off before admin
    runs += _run(NOW - timedelta(minutes=20), {"fetch": 30, "extract": 30, "enrich": 900, "export": 30})  # cut off again
    runs += _run(NOW - timedelta(minutes=12), full)  # this run, up to the admin step
    hist = [{"day": _day(i), "dbReadKb": 40 * 1024} for i in range(14, 7, -1)] + \
           [{"day": _day(i), "dbReadKb": 130 * 1024} for i in range(7, 0, -1)]
    admin = {"runs": runs, "history": hist, "schedule": {"runsPerDay": 72},
             "sources": [{"key": "reddit-ai", "name": "Reddit AI communities", "enabled": True, "errorCount": 300, "lastError": "403 Forbidden"}],
             "database": {"reads": {"quotaMB": 5120, "steps": [{"step": "rank", "readKB": 900}]}}}
    p = {f["key"]: f for f in team.platform(admin, NOW)["findings"]}
    assert p["skipped"]["text"].startswith("Steps skipped for time in the last 7 runs: images 4, topics 4"), p["skipped"]["text"]
    assert "Summaries (enrich) average" in p["skipped"]["text"] and p["skipped"]["impact"] == "readers", p["skipped"]
    assert p["cut-off"]["text"].startswith("2 of the last") and p["cut-off"]["impact"] == "outage", p["cut-off"]
    src = p["source:reddit-ai"]
    assert src["text"] == "Reddit AI communities has failed 300 runs in a row (about 4 days). Last error: 403 Forbidden.", src
    assert src["level"] == "watch" and "sources.yaml" in src["fix"]
    e = p["egress"]
    assert e["text"] == ("The pipeline read 130 MB a day from the database this week, up 225% on the week before: "
                         "3.8 GB a month at that rate, of the 5.0 GB free allowance. The latest run read most in rank."), e["text"]
    assert e["level"] == "watch" and e["owner"] == "Claude", e


def test_the_guard_reads_the_workflow_runs_github_reports():
    def wf(back_min, conclusion):
        return {"id": back_min, "status": "completed", "conclusion": conclusion,
                "created_at": (NOW - timedelta(minutes=back_min)).isoformat().replace("+00:00", "Z"), "html_url": f"https://x/{back_min}"}

    two = team.workflow_findings([wf(20, "failure"), wf(40, "timed_out"), wf(60, "success"),
                                  {"status": "in_progress", "conclusion": None, "created_at": NOW.isoformat()}], NOW, "Build site")
    assert two[0]["key"] == "workflow-failing" and two[0]["level"] == "act" and two[0]["impact"] == "outage", two
    assert two[0]["text"] == 'The last 2 workflow runs failed, the latest at 16:40 UTC in "Build site": the site is not being updated.', two
    assert two[0]["url"] == "https://x/20"
    one = team.workflow_findings([wf(20, "timed_out"), wf(40, "success")], NOW)
    assert one[0]["level"] == "watch" and "ran out of time" in one[0]["text"], one
    earlier = team.workflow_findings([wf(20, "success"), wf(200, "failure"), wf(300, "failure"), wf(400, "failure")], NOW)
    assert earlier[0]["key"] == "workflow-failed" and earlier[0]["text"].startswith("3 workflow runs of 4 failed"), earlier
    assert team.workflow_findings([wf(20, "success"), wf(60 * 30, "failure")], NOW) == []
    g = team.guard({"runs": []}, NOW, 0.2, two)
    assert g["status"] == "act" and g["headline"].startswith("The last 2 workflow runs failed")


def test_the_finisher_keeps_items_open_until_they_are_gone_and_ages_them():
    owner = [{"key": "token", "text": "Make a new token.", "owner": "Martin", "level": "act", "done": False, "fix": "Settings."}]
    t1 = team.build(_admin(crashed=True), None, None, NOW, site_hours=390.0, owner_items=owner)
    led = t1["ledger"]
    assert "guard:site-stale" in led and "owner:token" in led and not led["guard:site-stale"]["closedAt"]
    assert led["owner:token"]["fix"] == "Settings."
    # What the pipeline handles itself is not a to-do: no bot wave or spent allowance in the ledger.
    assert not any(k.endswith(("bot-wave", "models-spent")) for k in led), list(led)
    # Eight days later the site is still stale: the item is the same one, now overdue.
    later = NOW + timedelta(days=8)
    t2 = team.build(_admin(crashed=True), None, t1, later, site_hours=580.0, owner_items=owner)
    fin = t2["roles"]["finisher"]
    assert t2["ledger"]["guard:site-stale"]["openedAt"] == led["guard:site-stale"]["openedAt"]
    assert fin["overdue"] >= 2 and fin["status"] == "act", fin
    assert any("Open 8 days (Martin): Make a new token." == f["text"] for f in fin["findings"]), fin["findings"]
    assert "Martin" in fin["headline"] and "Claude" in fin["headline"], fin["headline"]
    # The site is fixed and the token marked done: both close; info findings never enter the ledger.
    owner[0]["done"] = True
    t3 = team.build(_admin(), None, t2, later + timedelta(hours=1), site_hours=0.2, owner_items=owner)
    assert t3["ledger"]["guard:site-stale"]["closedAt"] and t3["ledger"]["owner:token"]["closedAt"]
    assert not any(k.startswith("market:referrers") for k in t3["ledger"])
    assert t3["roles"]["finisher"]["closed7"] >= 2
    # The site goes stale again an hour later: the same row comes back, not a second one.
    t4 = team.build(_admin(), None, t3, later + timedelta(hours=2), site_hours=3.5, owner_items=owner)
    row = t4["ledger"]["guard:site-stale"]
    assert not row["closedAt"] and row["openedAt"] == led["guard:site-stale"]["openedAt"] and row["reopened"] == 1, row


def test_rows_from_the_old_ledger_that_nobody_owns_leave():
    old = {"format": 1, "roles": {}, "ledger": {
        "guard:bot-wave": {"key": "guard:bot-wave", "role": "guard", "text": "x", "level": "watch", "owner": "pipeline",
                           "openedAt": "2026-11-20T00:00:00Z", "seenAt": "2026-11-20T00:00:00Z", "closedAt": None}}}
    t = team.build({}, None, old, NOW, site_hours=0.1)
    assert "guard:bot-wave" not in t["ledger"]


def test_roles_keep_their_rhythm_and_a_closed_item_needs_its_role_to_run_again():
    t1 = team.build(_admin(), None, None, NOW, site_hours=0.1)
    assert set(t1["ranNow"]) == set(team.ORDER)
    t2 = team.build(_admin(), None, t1, NOW + timedelta(minutes=20), site_hours=0.1)
    assert "market" not in t2["ranNow"] and "ideas" not in t2["ranNow"] and "guard" in t2["ranNow"]
    assert t2["roles"]["market"]["ranAt"] == t1["roles"]["market"]["ranAt"]
    # Market's "found nothing" search stays open while market has not run again.
    assert not t2["ledger"]["market:search-missing"]["closedAt"]


def test_market_remembers_where_readers_came_from_and_sees_a_channel_drop():
    a = _admin()
    week_ago = team.build(a, None, None, NOW - timedelta(days=7), site_hours=0.1)
    a2 = _admin()
    a2["engagement"]["sources7"] = [{"name": "Direct", "visitors": 180}, {"name": "ChatGPT", "visitors": 12}, {"name": "Bluesky", "visitors": 4}]
    t = team.build(a2, None, week_ago, NOW, site_hours=0.1)
    m = {f["key"]: f for f in t["roles"]["market"]["findings"]}
    assert m["referrers"]["text"] == "Readers this week came from ChatGPT 12 (a week ago 41), Bluesky 4 (none a week ago).", m["referrers"]["text"]
    drop = m["referrer-drop:chatgpt"]
    assert drop["text"] == "ChatGPT sent 12 readers this week, down from 41 a week before." and drop["impact"] == "readers", drop
    assert set(t["roles"]["market"]["referrers"]) == {(NOW - timedelta(days=7)).date().isoformat(), NOW.date().isoformat()}


def test_the_brain_puts_what_moves_readers_before_the_machinery():
    owner = [{"key": "github-token", "text": "Make a new token.", "owner": "Martin", "level": "act", "impact": "ops", "fix": "Settings."},
             {"key": "bing-note", "text": "Send the Bing note.", "owner": "Martin", "level": "watch", "impact": "readers", "weight": 30, "fix": "Send it."},
             {"key": "kit", "text": "Confirm Kit.", "owner": "Martin", "level": "watch", "impact": "readers", "weight": 15}]
    t = team.build({"engagement": {"perDay": []}}, None, None, NOW, site_hours=0.1, owner_items=owner)
    plan = t["roles"]["brain"]["plan"]
    assert [p["text"] for p in plan] == ["Send the Bing note.", "Confirm Kit.", "Make a new token."], plan
    assert plan[0]["fix"] == "Send it." and plan[2]["why"] == team.WHY["ops"], plan
    # A site that stopped comes before everything.
    t2 = team.build({"engagement": {"perDay": []}}, None, None, NOW, site_hours=9.0, owner_items=owner)
    assert t2["roles"]["brain"]["plan"][0]["impact"] == "outage"


def test_ideas_come_from_a_free_model_and_never_bring_numbers_nobody_measured():
    asked = []

    def model(prompt):
        asked.append(prompt)
        return {"ideas": [
            {"title": "Answer the questions readers search for", "why": "Readers searched for sora pricing and found nothing.", "firstStep": "Add a pricing answer."},
            {"title": "Promise 10x traffic", "why": "It will bring many readers.", "firstStep": "Post it."},
            {"title": "Status page", "why": "It builds trust with the 4 daily readers.", "firstStep": "Add a banner."},
            {"title": "Weekly ChatGPT digest page", "why": "ChatGPT sends the most readers.", "firstStep": "Write the page."},
        ]}

    t = team.build(_admin(), None, None, NOW, site_hours=0.1, call=model)
    titles = [i["title"] for i in t["roles"]["ideas"]["ideas"]]
    assert titles == ["Answer the questions readers search for", "Weekly ChatGPT digest page"], titles
    assert "ChatGPT 41" in asked[0] and "none" in asked[0]
    assert t["roles"]["ideas"]["findings"][0]["fix"] == "Add a pricing answer."
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
