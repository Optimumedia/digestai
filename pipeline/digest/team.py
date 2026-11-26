"""The AI team: six roles that read what the run already measured and keep the site moving.

Martin asked (23 Nov 2026) for a team on the admin page, each role with its own job, and a picture
of how they work together. Every role is a set of rules over data this run already has (admin.json,
gsc.json, the live site's own API file, and the workflow's own run list from GitHub), so the team
adds no database reads. One role may ask a free model for words: the ideas role once a week, and
nothing else depends on it.

- Guard (health and truth): is the live site actually changing, did a workflow run or a step fail,
  are the reader numbers honest? Added because from 7 to 23 Nov 2026 every run was green while the
  export crashed and the site stood still for 16 days, and in October a wave of automated visits
  looked like readers.
- Platform: run time against the run's budget, steps skipped for time, runs cut off, failing
  sources (for how many days), free model allowances, database size and the egress trend.
- Market: where readers come from and how each channel moved on the week before, what they search
  for and do not find, what Google shows the site for without a click.
- Ideas: three zero-cost ideas a week no one asked for, grounded in the other roles' facts.
- Finisher: a ledger of every open finding someone has to act on (Martin or Claude), with its age;
  nothing is dropped quietly. Findings the pipeline handles itself stay off it. An item closes only
  when the role that raised it runs again and no longer finds it; one that comes back within a day
  is the same item, not a new one.
- Brain (team lead): reads everyone, tracks readers a day (complete days, readers only) against the
  week before, and picks today's three priorities: what stops readers getting news first, then what
  brings readers, then the rest, each with its owner and its fix.

Every finding carries the exact number it is about and, when someone has to act, the fix. Each role
has its own rhythm (EVERY_HOURS); a role that is not due keeps its last output. The result goes to
site/src/data/team.json (the /admin/team page) and the runner's cache (the ledger's memory).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

log = logging.getLogger("digest.team")

FORMAT = 2
# Open items that wait on a person, edited by hand like moderation.yaml: {"items": [{"key", "text",
# "owner", "level", "done", "impact", "fix", "why", "weight"}]}. The Finisher keeps each one open
# until it is marked done there.
ITEMS_FILE = Path(__file__).with_name("team_items.json")
EVERY_HOURS = {"guard": 0, "platform": 1, "market": 6, "ideas": 24 * 7, "finisher": 0, "brain": 0}
ORDER = ["brain", "guard", "platform", "market", "ideas", "finisher"]
# Who reads whose work: the arrows on the team page.
HANDOFFS = [
    ("guard", "brain", "incidents first"),
    ("platform", "brain", "what to fix"),
    ("market", "brain", "what readers want"),
    ("ideas", "brain", "ideas to try"),
    ("market", "ideas", "facts to build on"),
    ("guard", "finisher", "every finding"),
    ("platform", "finisher", "every finding"),
    ("market", "finisher", "every finding"),
    ("finisher", "brain", "what is overdue"),
    ("brain", "owners", "today's plan"),
]
ROLES = {
    "brain": ("Brain", "Team lead",
              "Reads every role, tracks readers a day against last week, and sets today's three priorities: what most moves real readers."),
    "guard": ("Guard", "Health and truth",
              "Checks the live site is changing, no workflow run or step failed, and reader numbers leave automated visits out."),
    "platform": ("Platform", "Platform improvements",
                 "Watches run time, steps skipped for time, failing sources, free model allowances, database size and egress."),
    "market": ("Market", "Market knowledge",
               "Where readers come from and how each channel moved, what they search for, and what Google shows the site for."),
    "ideas": ("Ideas", "Out-of-the-box ideas",
              "Once a week, three zero-cost ideas nobody asked for, built on the other roles' facts."),
    "finisher": ("Finisher", "Nothing left unfinished",
                 "Keeps every finding someone has to act on, with its age and owner, and closes it only when it is really gone."),
}
SITE_STALE_HOURS = 3.0
SOURCE_ERRORS = 5
OVERDUE_DAYS = 7
KEEP_CLOSED_DAYS = 14
REOPEN_HOURS = 24  # an item back within a day of closing is the same item (a flapping check)
IDEAS_KEPT = 30
REF_DAYS_KEPT = 15  # Market's own memory of where readers came from, one snapshot a day
LEVELS = {"act": 0, "watch": 1, "info": 2}
# What a finding does to readers, for the Brain's order: readers get no news, readers come or stay,
# or the machinery behind it (costs, limits, buttons).
IMPACT = {"outage": 0, "readers": 1, "ops": 2}
PEOPLE = ("Martin", "Claude")
ROLE_RANK = {"guard": 0, "platform": 1, "market": 2, "ideas": 3, "finisher": 4, "owner": 5}
# The steps that put new stories on the site: a crash in one of these keeps readers on old news.
SITE_STEPS = {"fetch", "extract", "gate", "enrich", "cluster", "rank", "export"}
PROVIDERS = {"cloud": "Ollama Cloud", "gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter",
             "mistral": "Mistral", "cloudflare": "Cloudflare"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def n(x) -> str:
    return f"{int(round(x)):,}"


def plural(k: int | float, word: str, many: str | None = None) -> str:
    return f"{n(k)} {word if round(k) == 1 else (many or word + 's')}"


def mb(v: float) -> str:
    return f"{v / 1024:.1f} GB" if v >= 1024 else f"{v:,.0f} MB"


def short_day(d: str) -> str:
    try:
        dd = datetime.fromisoformat(d)
    except (TypeError, ValueError):
        return str(d)
    return f"{dd.day} {dd:%b}"


def finding(key: str, text: str, level: str = "info", owner: str = "pipeline", fix: str | None = None,
            impact: str = "ops", why: str | None = None, url: str | None = None, weight: float = 0) -> dict:
    """One thing a role found. `text` has the number, `fix` what to do (for Martin or Claude), `why`
    what it does to readers, `weight` how many readers are at stake where that is known (it only
    orders the Brain's plan and is never shown)."""
    out = {"key": key, "text": text, "level": level, "owner": owner, "impact": impact}
    if fix:
        out["fix"] = fix
    if why:
        out["why"] = why
    if url:
        out["url"] = url
    if weight:
        out["weight"] = round(float(weight), 1)
    return out


def role_out(rid: str, now: datetime, findings: list[dict], headline: str, **more) -> dict:
    worst = min((LEVELS[f["level"]] for f in findings), default=2)
    status = {0: "act", 1: "watch", 2: "ok"}[worst]
    name, title, job = ROLES[rid]
    return {"id": rid, "name": name, "title": title, "job": job, "ranAt": _iso(now), "status": status,
            "headline": headline, "findings": findings, **more}


def actions_url() -> str:
    return f"https://github.com/{os.environ.get('GITHUB_REPOSITORY') or 'Optimumedia/digestai'}/actions/workflows/pipeline.yml"


# ---------------------------------------------------------------------------- reading admin.json

def run_groups(admin: dict) -> list[list[dict]]:
    """The step rows in admin.json as pipeline runs, oldest first: a run starts at fetch or after a
    29-minute gap (the dashboard's own rule, admin.group_runs)."""
    rows = sorted((s for s in admin.get("runs") or [] if _parse(s.get("startedAt"))), key=lambda s: s["startedAt"])
    groups: list[list[dict]] = []
    for s in rows:
        at = _parse(s["startedAt"])
        if groups and s.get("step") != "fetch" and at - _parse(groups[-1][0]["startedAt"]) < timedelta(minutes=29):
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups


def latest_run(admin: dict) -> list[dict]:
    """The step rows of the newest run in admin.json: this run, up to the admin step."""
    groups = run_groups(admin)
    return groups[-1] if groups else []


def _steps(g: list[dict]) -> set[str]:
    return {s.get("step") for s in g}


def _seconds(g: list[dict]) -> float:
    return sum(float((s.get("stats") or {}).get("seconds") or 0) for s in g)


def complete_runs(admin: dict) -> list[list[dict]]:
    """Runs that reached the admin step, oldest first. The newest of them is this run (its admin
    row is written before the step runs), so run times use the ones before it."""
    return [g for g in run_groups(admin) if g[0].get("step") == "fetch" and "admin" in _steps(g)]


def readers_per_day(admin: dict, today: str | None = None) -> list[tuple[str, int]]:
    """(day, readers) for complete days, oldest first. From the daily history (admin.json history:
    visitors as recorded, the likely automated visits beside them, so readers are the difference,
    as on the dashboard), which goes back months; days the counter could not record and today, still
    running, are left out. Falls back to the engagement's two weeks when there is no history."""
    gaps = {d for g in admin.get("counterGaps") or [] for d in _days(g.get("from"), g.get("to"))}
    today = today or ((admin.get("days") or [None])[-1]) or (admin.get("generatedAt") or "")[:10]
    hist = admin.get("history") or []
    out: list[tuple[str, int]] = []
    if hist:
        for r in sorted(hist, key=lambda r: r.get("day") or ""):
            d = r.get("day")
            if not d or d >= today or d in gaps or r.get("noData"):
                continue
            visitors = r.get("visitors") if r.get("visitors") is not None else r.get("sessions")
            if visitors is None:
                continue
            out.append((d, max(0, int(visitors) - int(r.get("automatedVisits") or 0))))
        return out
    rows = ((admin.get("engagement") or {}).get("perDay")) or []
    return [(r["day"], int(r.get("visitors") or 0)) for r in rows
            if r.get("day") and r["day"] < today and r["day"] not in gaps and r.get("visitors") is not None]


def week_on_week(days: list[tuple[str, float]]) -> tuple[float | None, float | None]:
    """(average of the last 7 days, average of the 7 before), None where there are not 7 days."""
    vals = [v for _d, v in days]
    week, before = vals[-7:], vals[-14:-7]
    return (sum(week) / 7 if len(week) == 7 else None), (sum(before) / 7 if len(before) == 7 else None)


def change(now_v: float | None, before: float | None) -> float | None:
    return None if now_v is None or not before else (now_v - before) / before * 100


def updown(pct: float) -> str:
    return f"{'up' if pct >= 0 else 'down'} {abs(pct):.0f}%"


def _days(a, b) -> list[str]:
    try:
        d, end = datetime.fromisoformat(a).date(), datetime.fromisoformat(b).date()
    except (TypeError, ValueError):
        return []
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _card_text(c: dict) -> str:
    at = c.get("at")
    return str(c.get("what") or "").replace("{at}", f"{at[11:16]} UTC" if at else "an earlier time")


# ---------------------------------------------------------------------------- the workflow's own runs

def workflow_findings(runs: list[dict] | None, now: datetime, failed_step: str | None = None) -> list[dict]:
    """Failed workflow runs in the last 24 hours, from GitHub's run list (fetched by run()). The
    pipeline's own records only see steps that crash; a run that fails in the site build, the
    deployment or the job's time limit is only visible here."""
    if not runs:
        return []
    day = [r for r in runs if r.get("status") == "completed" and (_parse(r.get("created_at")) or now) >= now - timedelta(hours=24)]
    day.sort(key=lambda r: r.get("created_at") or "")
    bad = [r for r in day if r.get("conclusion") in ("failure", "timed_out", "startup_failure")]
    if not bad:
        return []
    last = bad[-1]
    streak = 0
    for r in reversed(day):
        if r.get("conclusion") in ("failure", "timed_out", "startup_failure"):
            streak += 1
        elif r.get("conclusion") == "success":
            break
    when = (last.get("created_at") or "")[11:16]
    where = f' in "{failed_step}"' if failed_step else ""
    how = "ran out of time" if last.get("conclusion") == "timed_out" else "failed"
    url = last.get("html_url") or actions_url()
    if streak >= 2:
        return [finding("workflow-failing", f"The last {streak} workflow runs failed, the latest at {when} UTC{where}: the site is not being updated.",
                        "act", "Claude", f"Open the failed run's log, fix the step that failed{where}, then start a run.",
                        "outage", "Readers see the same news until a run gets through.", url)]
    if streak == 1:
        return [finding("workflow-failing", f"The latest workflow run {how} at {when} UTC{where}.",
                        "watch", "Claude", "Open its log; if the next run fails the same way, fix that step.",
                        "outage", "New stories wait for the next run.", url)]
    return [finding("workflow-failed", f"{plural(len(bad), 'workflow run')} of {len(day)} failed in the last 24 hours, the latest at {when} UTC{where}; later runs worked.",
                    "watch" if len(bad) >= 3 else "info", "Claude",
                    "Open the latest failed run's log; failures that come back need a fix." if len(bad) >= 3 else None,
                    "ops", url=url)]


# ---------------------------------------------------------------------------- the roles

# Today-tab cards the other roles already report in their own words.
COVERED_CARDS = ("source:", "readers:automated", "database:", "runs:", "pipeline:silent")


def guard(admin: dict, now: datetime, site_hours: float | None, workflow: list[dict] | None = None) -> dict:
    f: list[dict] = []
    if site_hours is not None and site_hours > SITE_STALE_HOURS:
        f.append(finding("site-stale", f"The live site was last generated {site_hours:.1f} hours ago (the limit is {SITE_STALE_HOURS:.0f}).",
                         "act", "Claude", "Open the latest run in the Actions log: when the export or the site build fails, fix that step and start a run.",
                         "outage", "Readers see old news and the search engines see a site that stopped.", actions_url()))
    f += workflow or []
    run = latest_run(admin)
    crashed = [s["step"] for s in run if (s.get("stats") or {}).get("crashed")]
    if crashed:
        site = [s for s in crashed if s in SITE_STEPS]
        f.append(finding("crashed", f"Crashed in the latest run: {', '.join(crashed)}.", "act" if site else "watch", "Claude",
                         "Open the run's log for the error and fix the step; the rest of the run carried on.",
                         "outage" if site else "ops",
                         "A crash in these steps keeps new stories off the site." if site else None, actions_url()))
    # Crashes across the runs admin.json holds (the last few hours), by step: one bad run is noise, the
    # same step crashing run after run is what froze the site in November.
    groups = run_groups(admin)[-12:]
    by_step: dict[str, int] = {}
    for g in groups:
        for s in g:
            if (s.get("stats") or {}).get("crashed"):
                by_step[s["step"]] = by_step.get(s["step"], 0) + 1
    repeat = sorted(((k, v) for k, v in by_step.items() if v >= 3), key=lambda kv: -kv[1])
    if repeat:
        step, times = repeat[0]
        f.append(finding("crash-repeat", f"{step} crashed in {times} of the last {len(groups)} runs.",
                         "act" if step in SITE_STEPS else "watch", "Claude",
                         f"Read the {step} step's error in the Actions log; a crash that repeats is a bug, not bad luck.",
                         "outage" if step in SITE_STEPS else "ops", url=actions_url()))
    # And by day, from the daily history: yesterday's crashed steps against the days before.
    hist = {r.get("day"): r for r in admin.get("history") or []}
    y = (now.date() - timedelta(days=1)).isoformat()
    crashed_y = int((hist.get(y) or {}).get("crashedSteps") or 0)
    if crashed_y >= 10 and not crashed and not repeat:
        f.append(finding("crashes-yesterday", f"{plural(crashed_y, 'step')} crashed yesterday ({short_day(y)}); the latest run was clean.",
                         "info", "Claude"))
    days = readers_per_day(admin, now.date().isoformat())
    if len(days) >= 2 and all(v == 0 for _d, v in days[-2:]) and any(v for _d, v in days[:-2]):
        f.append(finding("counter-silent", "No reader was counted for two days: the visitor counter may be blocked.",
                         "act", "Claude", "Open the site with the browser's network panel: the counter's request to Supabase should answer 201. "
                         "A 402 or 429 means the free plan's quota; anything else is a bug in the counter.", "ops",
                         "Without counts the ranking and every reader number go blind."))
    eng = admin.get("engagement") or {}
    auto = int(((eng.get("automated7") or {}).get("visitors")) or 0)
    week = sum(v for _d, v in days[-7:])
    if auto and auto > 3 * max(week, 1):
        f.append(finding("bot-wave", f"Automated visits outnumber readers this week, {n(auto)} to {n(week)}. "
                                     "Reader numbers, the ranking and this plan leave them out."))
    cards = [c for c in admin.get("actions") or [] if c.get("level") in ("critical", "warning")
             and not str(c.get("id") or "").startswith(COVERED_CARDS)]
    for c in cards[:3]:
        cid = str(c.get("id") or "card")
        imp = ("outage" if cid.startswith("pipeline") else
               "readers" if cid.startswith(("search", "quality", "work", "summaries")) else "ops")
        # Cards about the machinery are Claude's to fix; the rest (Search Console, editorial checks,
        # held stories) need Martin's hands or judgement.
        who = "Claude" if cid.startswith(("pipeline", "backup", "media", "summaries", "work", "history", "extract")) else "Martin"
        first = next((str(i.get("headline")) for i in c.get("items") or [] if isinstance(i, dict) and i.get("headline")), None)
        text = _card_text(c) + (f" First: {first[:80]}." if first else "")
        f.append(finding(f"card:{cid}", text, "act" if c["level"] == "critical" else "watch", who,
                         str(c.get("todo") or "") or None, imp, str(c.get("why") or "") or None, "/admin#today"))
    head = next((x["text"] for x in f if x["level"] != "info"), None) or \
        "The site is live and changing, no run or step failed, and reader numbers leave automated visits out."
    return role_out("guard", now, f, head, siteHours=site_hours)


def platform(admin: dict, now: datetime) -> dict:
    f: list[dict] = []
    budget = float(config.RUN_BUDGET_SECONDS)
    done = complete_runs(admin)
    finished = done[:-1] if len(done) > 1 else done  # the newest is this run, not finished yet
    last = finished[-1] if finished else latest_run(admin)
    seconds = _seconds(last)
    slow = sorted(((s.get("step"), float((s.get("stats") or {}).get("seconds") or 0)) for s in last), key=lambda kv: -kv[1])[:2]
    slow_text = " and ".join(f"{k} {v / 60:.1f}" for k, v in slow if v >= 60)
    if seconds > budget * 0.85:
        f.append(finding("run-time", f"The last full run took {seconds / 60:.1f} of its {budget / 60:.0f} budgeted minutes"
                                     + (f"; the slowest: {slow_text} minutes." if slow_text else "."),
                         "act" if seconds > budget else "watch", "Claude",
                         f"Make {slow[0][0]} faster or give it less to do per run; above the budget the steps that can wait are skipped." if slow else None))
    # Steps that can wait (run.py CAN_WAIT) are skipped when a run is short of time; a skipped step
    # writes no row, so a complete run without one skipped it.
    from .run import CAN_WAIT

    recent = done[-12:]
    skipped: dict[str, int] = {}
    for g in recent:
        for step in set(CAN_WAIT) - _steps(g):
            skipped[step] = skipped.get(step, 0) + 1
    often = sorted(((k, v) for k, v in skipped.items() if v * 2 >= len(recent) and len(recent) >= 4), key=lambda kv: (-kv[1], kv[0]))
    if often:
        names = ", ".join(f"{k} {v}" for k, v in often[:5])
        avg = sum(_seconds(g) for g in recent) / len(recent)
        enrich = [float((s.get("stats") or {}).get("seconds") or 0) for g in recent for s in g if s.get("step") == "enrich"]
        heavy = f" Summaries (enrich) average {sum(enrich) / len(enrich) / 60:.1f} minutes a run." if enrich else ""
        f.append(finding("skipped", f"Steps skipped for time in the last {len(recent)} runs: {names} (runs average {avg / 60:.1f} minutes).{heavy}",
                         "watch", "Claude",
                         "Shorten the slowest step (fewer summaries per run, or a faster fallback model), so the skipped steps get their turn.",
                         "readers" if any(k in ("topics", "social", "newsletter", "upgrade") for k, _ in often) else "ops",
                         "Skipped steps mean stale topic pages, fewer posts and weaker summaries."))
    groups = run_groups(admin)
    cut = [g for g in groups[:-1] if g[0].get("step") == "fetch" and "admin" not in _steps(g)]
    if len(cut) >= 2:
        f.append(finding("cut-off", f"{len(cut)} of the last {len(groups) - 1} runs stopped before the dashboard step: the job's time limit cut them off.",
                         "watch", "Claude", "Find what made those runs slow in the Pipeline tab's run table, and give that step a time limit of its own.",
                         "outage", "A run cut off before the export publishes nothing new.", actions_url()))
    per_day = max(1, int((admin.get("schedule") or {}).get("runsPerDay") or config.RUNS_PER_DAY))
    failing = sorted((s for s in admin.get("sources") or [] if s.get("enabled") and not s.get("discovered")
                      and int(s.get("errorCount") or 0) >= SOURCE_ERRORS), key=lambda s: -int(s.get("errorCount") or 0))
    for s in failing[:5]:
        runs_n = int(s["errorCount"])
        days = runs_n / per_day
        since = f"about {days:.0f} days" if days >= 1.5 else "about a day" if days >= 0.75 else "a few hours"
        err = f" Last error: {str(s.get('lastError'))[:90]}." if s.get("lastError") else ""
        f.append(finding(f"source:{s.get('key')}", f"{s.get('name') or s.get('key')} has failed {plural(runs_n, 'run')} in a row ({since}).{err}",
                         "watch" if days >= 1 else "info", "Claude",
                         "Fix its address in pipeline/digest/sources.yaml, or pause it with the button on the Today tab.",
                         "ops", "No articles arrive from it, and every run spends time retrying it."))
    today = now.date().isoformat()
    spent = sorted({PROVIDERS.get(u["provider"], u["provider"]) for u in (admin.get("llm") or {}).get("usage") or []
                    if u.get("day") == today and u.get("exhausted")})
    if spent:
        f.append(finding("models-spent", f"Free model allowance used up today: {', '.join(spent)}. The other models take over; the allowances return at midnight UTC."))
    dbs = admin.get("database") or {}
    size, limit = dbs.get("sizeMB"), dbs.get("limitMB")
    if size and limit and size > 0.8 * limit:
        f.append(finding("db-size", f"The database holds {size:.0f} of {limit:.0f} MB.", "act" if size > 0.95 * limit else "watch", "Claude",
                         "Clear old article text sooner (the tidy step's age limit).", "ops", "At the limit no new story can be saved."))
    f += egress(admin, now)
    head = next((x["text"] for x in f if x["level"] != "info"), None) or \
        f"The last full run took {seconds / 60:.1f} of {budget / 60:.0f} minutes; sources, models and database are within limits."
    return role_out("platform", now, f, head, runMinutes=round(seconds / 60, 1))


def egress(admin: dict, now: datetime) -> list[dict]:
    """What the pipeline reads from the database a day (the daily history's dbReadKb, complete days),
    this week against the week before and against the free plan's monthly allowance. Supabase's own
    usage page also counts the site's reads, so this is the pipeline's share."""
    dbs = admin.get("database") or {}
    quota = float(((dbs.get("reads") or {}).get("quotaMB")) or config.SUPABASE_EGRESS_GB * 1024)
    today = now.date().isoformat()
    days = [(r["day"], float(r["dbReadKb"]) / 1024) for r in sorted(admin.get("history") or [], key=lambda r: r.get("day") or "")
            if r.get("day") and r["day"] < today and r.get("dbReadKb") is not None]
    out: list[dict] = []
    week, before = week_on_week(days)
    if week is not None:
        month = week * 30
        moved = change(week, before)
        trend = f", {updown(moved)} on the week before" if moved is not None and abs(moved) >= 10 else ""
        heavy = ((dbs.get("reads") or {}).get("steps") or [{}])[0]
        most = f" The latest run read most in {heavy['step']}." if heavy.get("step") else ""
        level = "act" if month > 0.9 * quota else "watch" if month > 0.7 * quota else "info"
        out.append(finding("egress", f"The pipeline read {week:,.0f} MB a day from the database this week{trend}: "
                                     f"{mb(month)} a month at that rate, of the {mb(quota)} free allowance.{most}",
                           level, "Claude" if level != "info" else "pipeline",
                           "Find the step that reads the most on the Pipeline tab and make it read only what changed." if level != "info" else None,
                           "ops", "Over the allowance Supabase blocks the site's visitor counter, as it did 4 to 10 Oct." if level != "info" else None))
    projected = ((dbs.get("cycle") or {}).get("projectedMB"))
    if projected and projected > 0.8 * quota and not any(x["level"] != "info" for x in out):
        out.append(finding("egress-cycle", f"Database reads are heading for {mb(projected)} of {mb(quota)} this billing cycle.",
                           "act" if projected > quota else "watch", "Claude",
                           "Find the step that reads the most on the Pipeline tab and make it read only what changed.", "ops"))
    return out


# Referrers the reader numbers group as search engines or as our own posts are still shown by name.
def market(admin: dict, gsc: dict | None, now: datetime, prev: dict | None = None) -> dict:
    f: list[dict] = []
    eng = admin.get("engagement") or {}
    refs = [s for s in eng.get("sources7") or [] if s.get("name") not in ("Direct", None) and s.get("visitors")]
    today = now.date().isoformat()
    # Market's own memory: the week's readers per referrer, one snapshot a day, so it can say how a
    # channel moved without asking the database for last week.
    memory = {d: v for d, v in ((prev or {}).get("referrers") or {}).items() if d >= (now - timedelta(days=REF_DAYS_KEPT)).date().isoformat()}
    if eng.get("sources7"):
        memory[today] = {s["name"]: int(s.get("visitors") or 0) for s in eng["sources7"] if s.get("name")}
    week_ago = next((memory[d] for d in sorted(memory, reverse=True)
                     if 6 <= (now.date() - datetime.fromisoformat(d).date()).days <= 8), None)
    if refs:
        parts = []
        for s in refs[:5]:
            was = (week_ago or {}).get(s["name"], 0)
            then = "" if week_ago is None or was == s["visitors"] else " (none a week ago)" if not was else f" (a week ago {was})"
            parts.append(f"{s['name']} {s['visitors']}{then}")
        f.append(finding("referrers", "Readers this week came from " + ", ".join(parts) + "."))
    if week_ago:
        now_by = {s["name"]: int(s.get("visitors") or 0) for s in eng.get("sources7") or []}
        for name, was in sorted(week_ago.items(), key=lambda kv: -kv[1]):
            if name == "Direct" or was < 5:
                continue
            got = now_by.get(name, 0)
            if got <= was / 2:
                f.append(finding(f"referrer-drop:{name.lower()}", f"{name} sent {plural(got, 'reader')} this week, down from {was} a week before.",
                                 "watch", "Claude", f"Open the pages {name} sends readers to and check they load and answer what the link promised; "
                                 "if a post or listing was taken down, put it back.", "readers",
                                 f"{name} was one of the biggest ways readers found the site.", weight=was - got))
                break
    searches = (eng.get("searches7") or {})
    missing = [s for s in searches.get("missing") or [] if int(s.get("visitors") or 0) >= 2][:4]
    if missing:
        f.append(finding("search-missing", "Readers searched the site and found nothing for: "
                         + ", ".join(f'"{s["query"]}" ({plural(int(s["visitors"]), "reader")})' for s in missing) + ".",
                         "watch", "Claude", "Add the words to the matching topic page, or write the story they were after if it is AI news.",
                         "readers", "A search that finds nothing is a reader who leaves.", "/admin#readers",
                         weight=sum(int(s["visitors"]) for s in missing)))
    top = [s["query"] for s in searches.get("top") or [] if s.get("results") and len(s["query"]) <= 40][:4]
    if top:
        f.append(finding("search-top", "Most searched on the site: " + ", ".join(f'"{q}"' for q in top) + "."))
    pages = [p for p in eng.get("pages7") or [] if p.get("path") not in ("/", "/index.html")][:3]
    if pages:
        f.append(finding("pages-read", "Most read pages: " + ", ".join(f"{p['path']} ({plural(int(p.get('visitors') or 0), 'reader')})" for p in pages) + "."))
    if gsc and gsc.get("queries"):
        q = sorted(gsc["queries"], key=lambda r: -float(r.get("impressions") or 0))
        shown = [r for r in q if float(r.get("impressions") or 0) >= 20 and not float(r.get("clicks") or 0)][:3]
        if shown:
            def pos(r) -> str:
                p = float(r.get("position") or 0)
                return f", position {p:.0f}" if p else ""
            f.append(finding("search-shown", "Google showed the site without a click for "
                             + ", ".join(f'"{r["query"]}" ({n(float(r["impressions"]))} times{pos(r)})' for r in shown) + ".",
                             "watch", "Claude", f'Find the page that ranks for "{shown[0]["query"]}" on the Search tab and make its title and first line answer that search.',
                             "readers", "These searchers already see the site; a better title turns them into readers.", "/admin#seo",
                             weight=sum(float(r["impressions"]) for r in shown) / 20))
        clicks = sum(float(r.get("clicks") or 0) for r in gsc.get("perDay") or [])
        f.append(finding("search-clicks", f"Google sent {plural(clicks, 'click')} from {short_day(gsc.get('start'))} to {short_day(gsc.get('end'))}."))
    head = next((x["text"] for x in f if x["level"] != "info"), None) or (f[0]["text"] if f else
                                                                           "Not enough reader data this week to say where readers come from.")
    return role_out("market", now, f, head, referrers=memory)


IDEAS_PROMPT = """You are the ideas member of a small team growing digestai.news, a free AI news site
that merges every outlet reporting the same news into one story with all sources linked.
Facts from the team this week:
{facts}

Propose exactly 3 unconventional ideas to bring more real readers, each doable at zero cost by a
developer in a day, without paid ads, fake accounts, spam or invented claims. Do not repeat these
earlier ideas: {seen}.
Answer only with JSON: {{"ideas": [{{"title": "...", "why": "...", "firstStep": "..."}}]}}
Plain words, no hype, one sentence each for why and firstStep. Write no numbers at all."""

# A number on its own ("4 daily readers"), not one inside a name ("GPT-5", "Llama 3.1" is caught too:
# an idea needs no version numbers).
_NUMBER = re.compile(r"(?<![\w-])\d[\d,.]*")


def ideas(facts: list[str], prev: dict | None, now: datetime, call=None) -> dict:
    """Three ideas a week from a free model, or the last week's when no model answered."""
    seen = [i["title"] for i in (prev or {}).get("log") or []]
    log_ = list((prev or {}).get("log") or [])
    got: list[dict] = []
    reason = None
    if call is None:
        reason = "no free model available"
    else:
        try:
            result = call(IDEAS_PROMPT.format(facts="\n".join(f"- {x}" for x in facts) or "- (none yet)",
                                              seen="; ".join(seen[-IDEAS_KEPT:]) or "none"))
            for i in (result or {}).get("ideas") or []:
                if not isinstance(i, dict) or not all(isinstance(i.get(k), str) and i[k].strip() for k in ("title", "why", "firstStep")):
                    continue
                text = " ".join(i[k] for k in ("title", "why", "firstStep"))
                if _NUMBER.search(text) or i["title"].strip() in seen:
                    continue  # a number the model put there (the facts' numbers are not its to repeat), or an idea already made
                got.append({k: i[k].strip()[:300] for k in ("title", "why", "firstStep")})
            got = got[:3]
            if not got:
                reason = "the model's ideas did not pass the checks"
        except Exception as exc:  # noqa: BLE001 - ideas are never worth a step
            reason = f"model failed: {str(exc)[:80]}"

    def as_finding(i: dict) -> dict:
        return finding("idea:" + i["title"].lower()[:60], f"{i['title']}: {i['why']}", "info", "Claude", i["firstStep"], "readers")

    if got:
        log_ = (log_ + [{"title": i["title"], "at": _iso(now)} for i in got])[-IDEAS_KEPT:]
        return role_out("ideas", now, [as_finding(i) for i in got], f"This week: {got[0]['title']}.", ideas=got, log=log_)
    old = (prev or {}).get("ideas") or []
    out = role_out("ideas", now, [as_finding(i) for i in old],
                   f"No new ideas this week ({reason})." if not old else f"Last week's ideas stand ({reason}).", ideas=old, log=log_)
    out["status"] = "idle" if not old else out["status"]
    return out


LEDGER_FIELDS = ("text", "level", "owner", "impact", "fix", "why", "url", "weight")


def finisher(roles: dict[str, dict], ran: set[str], ledger: dict, owner_items: list[dict], now: datetime) -> tuple[dict, dict]:
    """The ledger: every act or watch finding Martin or Claude has to act on, kept open until the role
    that raised it runs again without it. Owner items (team_items.json) stay open until marked done
    there. Findings the pipeline handles itself (a model out of allowance, a wave of automated
    visits already left out) are not to-dos and stay off it."""
    # Rows from before the ledger kept to people's items (format 1) leave without a trace.
    led = {k: dict(v) for k, v in (ledger or {}).items() if v.get("owner") in PEOPLE}
    stamp = _iso(now)
    live: dict[str, tuple[str, dict]] = {}
    for rid, r in roles.items():
        for x in r.get("findings") or []:
            if x["level"] in ("act", "watch") and x.get("owner") in PEOPLE:
                live[f"{rid}:{x['key']}"] = (rid, x)
    for item in owner_items:
        if item.get("key") and item.get("text") and not item.get("done"):
            live[f"owner:{item['key']}"] = ("owner", finding(item["key"], item["text"], item.get("level") or "watch",
                                                             item.get("owner") or "Martin", item.get("fix"), item.get("impact") or "ops",
                                                             item.get("why"), item.get("url"), item.get("weight") or 0))
    for key, (rid, x) in live.items():
        row = led.get(key)
        fields = {k: x.get(k) for k in LEDGER_FIELDS}
        closed = _parse((row or {}).get("closedAt"))
        if row is None or (closed and now - closed > timedelta(hours=REOPEN_HOURS)):
            led[key] = {"key": key, "role": rid, **fields, "openedAt": stamp, "seenAt": stamp, "closedAt": None}
        else:
            if closed:  # back within a day: the same item, a check that flaps, not a new one
                row["reopened"] = int(row.get("reopened") or 0) + 1
            row.update(fields, seenAt=stamp, closedAt=None)
    done_owner = {f"owner:{i['key']}" for i in owner_items if i.get("key") and i.get("done")}
    for key, row in led.items():
        if row.get("closedAt") or key in live:
            continue
        if row["role"] in ran or key in done_owner or row["role"] == "owner":
            row["closedAt"] = stamp
    keep = now - timedelta(days=KEEP_CLOSED_DAYS)
    led = {k: v for k, v in led.items() if not v.get("closedAt") or (_parse(v["closedAt"]) or now) >= keep}

    def age(v) -> float:
        return (now - (_parse(v["openedAt"]) or now)).total_seconds() / 86400

    open_ = sorted((v for v in led.values() if not v.get("closedAt")), key=lambda v: (LEVELS.get(v["level"], 2), -age(v)))
    overdue = [v for v in open_ if age(v) >= OVERDUE_DAYS]
    week = now - timedelta(days=7)
    closed = [v for v in led.values() if v.get("closedAt") and (_parse(v["closedAt"]) or now) >= week]
    f = [finding("overdue:" + v["key"], f"Open {age(v):.0f} days ({v['owner']}): {v['text']}", "act" if v["level"] == "act" else "watch",
                 v["owner"], v.get("fix"), v.get("impact") or "ops") for v in overdue[:5]]
    people = {p: sum(1 for v in open_ if v["owner"] == p) for p in PEOPLE}
    head = (f"{len(open_)} open ({', '.join(f'{p} {k}' for p, k in people.items() if k) or 'none'}), "
            f"{len(overdue)} older than a week, {len(closed)} closed in the last 7 days." if led else "Nothing open.")
    items = [{**v, "ageDays": round(age(v), 1)} for v in open_]
    items += sorted(({**v, "ageDays": round(age(v), 1)} for v in closed), key=lambda v: v["closedAt"], reverse=True)
    return role_out("finisher", now, f, head, items=items, open=len(open_), overdue=len(overdue), closed7=len(closed)), led


WHY = {"outage": "Readers get no new news until this is fixed.",
       "readers": "This is where more readers come from, or stay.",
       "ops": "Keeps the machinery working; it moves readers only indirectly."}


def plan_order(v: dict, age_days: float) -> tuple:
    """Readers first: what stops the news, then what brings or keeps readers, then the rest; within
    each, act before watch, the most readers at stake, the role that saw it first, the oldest."""
    return (IMPACT.get(v.get("impact") or "ops", 2), LEVELS.get(v.get("level"), 2), -float(v.get("weight") or 0),
            ROLE_RANK.get(v.get("role"), 9), -age_days)


def brain(roles: dict[str, dict], ledger: dict, admin: dict, now: datetime) -> dict:
    days = readers_per_day(admin, now.date().isoformat())
    avg, prev = week_on_week(days)
    goal: dict = {}
    lines: list[str] = []
    if avg is None and days:  # under two weeks of history: what there is
        week = [v for _d, v in days[-7:]]
        avg = sum(week) / len(week)
        goal = {"readersPerDay": round(avg, 1), "days": len(week)}
    elif avg is not None:
        goal = {"readersPerDay": round(avg, 1), "days": 7}
    if goal:
        goal.update(last=[{"day": d, "readers": v} for d, v in days[-14:]])
        moved = change(avg, prev)
        if moved is not None:
            goal.update(previous=round(prev, 1), changePct=round(moved))
            lines.append(f"Readers: {avg:.0f} a day over the last 7 complete days, {updown(moved)} on the week before ({prev:.0f}).")
        else:
            lines.append(f"Readers: {avg:.0f} a day over the last {goal['days']} complete days.")

    def age(v) -> float:
        return (now - (_parse(v["openedAt"]) or now)).total_seconds() / 86400

    open_ = [v for v in (ledger or {}).values() if not v.get("closedAt") and v.get("owner") in PEOPLE]
    open_.sort(key=lambda v: plan_order(v, age(v)))
    first = [v for v in open_ if (v.get("impact") or "ops") != "ops"][:3]
    rest = [v for v in open_ if v not in first]

    def item(v: dict) -> dict:
        return {"text": v["text"], "fix": v.get("fix"), "why": v.get("why") or WHY.get(v.get("impact") or "ops"),
                "owner": v["owner"], "role": v["role"], "level": v["level"], "impact": v.get("impact") or "ops",
                "url": v.get("url"), "ageDays": round(age(v), 1)}

    plan = [item(v) for v in first]
    idea = next(iter((roles.get("ideas") or {}).get("ideas") or []), None)
    if len(plan) < 3 and idea:
        plan.append({"text": f"Try an idea: {idea['title']}. {idea['why']}", "fix": idea["firstStep"], "why": WHY["readers"],
                     "owner": "Claude", "role": "ideas", "level": "info", "impact": "readers", "url": None, "ageDays": 0})
    plan += [item(v) for v in rest[:3 - len(plan)]]
    if plan:
        lines.append("Today: " + " ".join(f"{i}. {p['text']} ({p['owner']})" for i, p in enumerate(plan, 1)))
    else:
        lines.append("Nothing urgent: keep publishing and look at the ideas.")
    act = [r["name"] for r in roles.values() if r.get("status") == "act"]
    if act:
        lines.append(f"Needs action now: {', '.join(act)}.")
    f = [finding(f"plan:{i}", p["text"], p["level"] if p["level"] in LEVELS else "info", p["owner"], p.get("fix"), p.get("impact") or "ops")
         for i, p in enumerate(plan, 1)]
    head = lines[0] if lines else "Waiting for reader data."
    return role_out("brain", now, f, head, plan=plan, goal=goal, brief=lines)


# ---------------------------------------------------------------------------- the step

def _read(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    # A damaged runner copy can still be valid JSON of another kind: every file read here is a dict.
    return data if isinstance(data, dict) else default


def due(prev: dict | None, rid: str, now: datetime) -> bool:
    ran = _parse(((prev or {}).get("roles") or {}).get(rid, {}).get("ranAt"))
    return ran is None or (now - ran).total_seconds() >= EVERY_HOURS[rid] * 3600 - 300


def facts_for_ideas(roles: dict[str, dict]) -> list[str]:
    out = []
    for rid in ("brain", "market", "platform", "guard"):
        r = roles.get(rid) or {}
        out += [x["text"] for x in r.get("findings") or []][:4]
        if rid == "brain":
            out += (r.get("brief") or [])[:1]
    return [x for x in out if x][:14]


def build(admin: dict, gsc: dict | None, prev: dict | None, now: datetime, site_hours: float | None,
          owner_items: list[dict] | None = None, call=None, workflow: list[dict] | None = None) -> dict:
    """The whole team for this run; pure, so it can be tested without the network. `workflow` is
    workflow_findings() of GitHub's run list, when the step could read it."""
    old = (prev or {}).get("roles") or {}
    roles: dict[str, dict] = {}
    ran: set[str] = set()

    def take(rid, fn):
        if due(prev, rid, now) or rid not in old:
            roles[rid] = fn()
            ran.add(rid)
        else:
            roles[rid] = old[rid]

    take("guard", lambda: guard(admin, now, site_hours, workflow))
    take("platform", lambda: platform(admin, now))
    take("market", lambda: market(admin, gsc, now, old.get("market")))
    roles["brain"] = old.get("brain") or brain(roles, {}, admin, now)
    take("ideas", lambda: ideas(facts_for_ideas({**roles, "brain": roles["brain"]}), old.get("ideas"), now, call))
    roles["finisher"], ledger = finisher({k: v for k, v in roles.items() if k in ran}, ran, (prev or {}).get("ledger") or {},
                                         owner_items or [], now)
    ran.add("finisher")
    roles["brain"] = brain(roles, ledger, admin, now)
    ran.add("brain")
    return {"format": FORMAT, "generatedAt": _iso(now), "order": ORDER,
            "handoffs": [{"from": a, "to": b, "what": w} for a, b, w in HANDOFFS],
            "roles": {rid: roles[rid] for rid in ORDER}, "ranNow": sorted(ran), "ledger": ledger}


def _model_call(info: dict):
    """The ideas role's free model: the morning note's (Groq, within its daily allowance)."""
    from . import morning

    return morning._default_call(info)


def github_runs(now: datetime) -> tuple[list[dict] | None, str | None]:
    """The pipeline workflow's last 30 runs from GitHub, and the name of the step that failed in the
    newest failed one: two requests with the run's own token, under two seconds. (None, None) when
    there is no token or GitHub does not answer; the Guard then says nothing about workflow runs."""
    token, repo = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None, None
    import requests

    api = f"https://api.github.com/repos/{repo}/actions"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        r = requests.get(f"{api}/workflows/pipeline.yml/runs", params={"per_page": 30}, headers=headers, timeout=8)
        if r.status_code != 200:
            return None, None
        runs = [{k: x.get(k) for k in ("id", "status", "conclusion", "created_at", "html_url")} for x in r.json().get("workflow_runs") or []]
        failed = next((x for x in runs if x["status"] == "completed" and x["conclusion"] in ("failure", "timed_out")
                       and (_parse(x["created_at"]) or now) >= now - timedelta(hours=24)), None)
        step = None
        if failed:
            j = requests.get(f"{api}/runs/{failed['id']}/jobs", headers=headers, timeout=8)
            if j.status_code == 200:
                for job in j.json().get("jobs") or []:
                    step = next((s.get("name") for s in job.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")), None)
                    if step:
                        break
        return runs, step
    except (requests.RequestException, ValueError) as exc:
        log.warning("workflow runs not read: %s", str(exc)[:120])
        return None, None


def run() -> dict:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    cache_path = config.CACHE_DIR / "team.json"
    prev = _read(cache_path, None)
    admin = _read(config.SITE_DATA_DIR / "admin.json", None) or _read(config.CACHE_DIR / "admin.json", {})
    gsc = _read(config.SITE_DATA_DIR / "gsc.json", None)
    owner_items = _read(ITEMS_FILE, {}).get("items") or []
    site_hours = None
    workflow = None
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("TEAM_SITE_CHECK") == "1":
        from . import watchdog

        site_hours = watchdog.site_age(now)
        runs, failed_step = github_runs(now)
        workflow = workflow_findings(runs, now, failed_step) if runs is not None else None
    info: dict = {}
    call = None
    if due(prev, "ideas", now):
        try:
            call = _model_call(info)
        except Exception as exc:  # noqa: BLE001 - no model, no ideas
            info["reason"] = str(exc)[:80]
    team = build(admin or {}, gsc, prev, now, site_hours, owner_items, call, workflow)
    text = json.dumps(team, ensure_ascii=False, separators=(",", ":"))
    for path in (config.SITE_DATA_DIR / "team.json", cache_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    status = {rid: r["status"] for rid, r in team["roles"].items()}
    return {"ran": team["ranNow"], "status": status, "open": team["roles"]["finisher"].get("open"),
            "siteHours": site_hours, "workflowRuns": None if workflow is None else len(workflow),
            "model": info.get("provider") or info.get("reason"), "seconds": round(time.monotonic() - t0, 1)}
