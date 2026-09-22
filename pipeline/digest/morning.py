"""Step: the morning note, an editor's six sentences about the last 24 hours, written by the pipeline.

Once a day, on the first run at or after MORNING_NOTE_HOUR_UTC (05:00) that has not yet written
that day's note, the step writes six plain sentences for the owner:

1. what led the briefing and why (score, publishers, primary source);
2. what readers opened against what the ranking predicted, and the biggest surprise;
3. the source that earned or lost the most learned weight since the previous note, and, only when
   reader data changed AI at Work's featured pick or a top-three place, which card it moved;
4. one budget fact: database reads, or a summary model out of its allowance;
5. visitors yesterday against the day before, and where they came from;
6. one thing to decide, picked by rules from whatever is off.

Everything is computed by rules from what the run already has: admin.json, briefing.json and
stories.json, which the admin and export steps wrote minutes earlier. The database is asked only
two narrow, grouped questions (reader events per story in the last 24 hours, and yesterday's
visitors by referrer and time zone), once a day; the run records their reads as this step's
readKB like any other step. On every other run the step reads nothing from the database.

A model may reword the sentences, never the facts: the reworded note is kept only if every
sentence carries exactly the figures of its rules-written sentence and names nothing the rules
text does not (the idea of checks.py, made exact). Otherwise the rules text is the note.

Storage: the last 14 notes live in the runner's cache (pipeline/data/cache/morning/notes.json,
carried from run to run by the workflow like the rest of the read cache) and are written to
site/src/data/morning.json on every run for the admin page. Each note is also a GitHub issue
(notify.py), with the note's data embedded, so a lost cache is rebuilt from the issues instead
of a database table that every run would have to read.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from . import checks, config, db, primary
from .quality import numbers_in

log = logging.getLogger("digest.morning")

KEEP = 14
HOUR_UTC = int(os.environ.get("MORNING_NOTE_HOUR_UTC") or 5)
LABELS = [("briefing", "Briefing"), ("readers", "Readers"), ("sources", "Sources"),
          ("budget", "Budget"), ("growth", "Growth"), ("decide", "To decide")]
SEARCH_ENGINES = {"Google", "Bing", "DuckDuckGo"}
FAILING_DAYS = 3
LOW_DEPTH_VIEWS, LOW_DEPTH_PERCENT = 10, 30
NOTE_MARK = "morning-note"


# ---------------------------------------------------------------------------- small helpers

def n(x) -> str:
    """An integer with thousands separators: 5,120."""
    return f"{int(round(float(x))):,}"


def mb(x: float) -> str:
    return f"{x:,.1f}"


def plural(k: int, word: str, many: str | None = None) -> str:
    return f"{n(k)} {word if k == 1 else (many or word + 's')}"


def day_title(d: str) -> str:
    dd = date.fromisoformat(d)
    return f"{dd.day} {dd:%b %Y}"


def short_day(d: str) -> str:
    dd = date.fromisoformat(d)
    return f"{dd.day} {dd:%b}"


def title_for(d: str) -> str:
    return f"Morning note, {day_title(d)}"


def _iso(dt) -> str:
    return db.as_utc(dt).isoformat().replace("+00:00", "Z")


def _q(text: str) -> str:
    return f"“{(text or '').strip()}”"


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------- the gate

def due(now: datetime, notes: list[dict], hour: int = HOUR_UTC) -> bool:
    """True on the first run at or after `hour` UTC that has not written today's note."""
    today = now.date().isoformat()
    return now.hour >= hour and not any(x.get("day") == today for x in notes)


# ---------------------------------------------------------------------------- facts

def publishers_of(story: dict) -> set[str]:
    from .hold import registrable

    return {registrable(a.get("domain") or "") for a in story.get("articles") or []} - {""}


def primary_name(story: dict) -> str | None:
    """The company's own announcement when the story has one, else the first primary source."""
    arts = [a for a in story.get("articles") or [] if a.get("sourceType") == "primary"]
    a = next((a for a in arts if primary.announcement(a)), arts[0] if arts else None)
    return (a.get("source") or a.get("domain")) if a else None


def confirmed(story: dict) -> bool:
    """The export's rule (export.confirmed): two publishers, the company's own announcement, or a pin."""
    return bool(story.get("pinned") or primary.announced(story) or len(publishers_of(story)) >= 2)


def briefing_facts(briefing: dict | None, stories: list[dict]) -> dict | None:
    by_id = {s["id"]: s for s in stories}
    ids = [i for i in (briefing or {}).get("storyIds") or [] if i in by_id]
    if not ids:
        return None
    top = [by_id[i] for i in ids]
    lead = top[0]
    best = max(top, key=lambda s: s.get("score") or 0)
    # A single-outlet story that scored higher but gave way: the briefing puts confirmed stories first.
    passed = next((s for s in sorted(top, key=lambda s: -(s.get("score") or 0))
                   if (s.get("score") or 0) > (lead.get("score") or 0) and not confirmed(s)), None)
    return {
        "headline": lead.get("headline"), "slug": lead.get("slug"), "score": round(float(lead.get("score") or 0), 3),
        "publishers": len(publishers_of(lead)), "primary": primary_name(lead) if lead.get("hasPrimary") else None,
        "hasPrimary": bool(lead.get("hasPrimary")), "pinned": bool(lead.get("pinned")),
        "highest": best is lead,
        "passed": {"headline": passed.get("headline"), "score": round(float(passed.get("score") or 0), 3)} if passed else None,
        "size": len(top),
    }


def reader_events(conn, since) -> dict[int, dict]:
    """Per story, the last 24 hours: views, time on page, clicks to the source, and the average of
    each reading session's deepest point. Grouped in the database: one row per story and type."""
    ev = db.events.c
    out: dict[int, dict] = {}
    for sid, etype, count, total in conn.execute(
        select(ev.story_id, ev.type, func.count(), func.sum(ev.value))
        .where(ev.created_at >= since, ev.story_id.isnot(None), ev.type.in_(("view", "dwell", "click_source")))
        .group_by(ev.story_id, ev.type)
    ).all():
        row = out.setdefault(int(sid), {"views": 0, "dwell": 0.0, "dwellN": 0, "clicks": 0, "depth": None, "depthN": 0})
        if etype == "view":
            row["views"] += int(count)
        elif etype == "dwell":
            row["dwell"] += float(total or 0)
            row["dwellN"] += int(count)
        elif etype == "click_source":
            row["clicks"] += int(count)
    deepest = (select(ev.session, ev.story_id, func.max(ev.value).label("pct"))
               .where(ev.created_at >= since, ev.type == "depth", ev.story_id.isnot(None))
               .group_by(ev.session, ev.story_id).subquery())
    for sid, avg, reads in conn.execute(
        select(deepest.c.story_id, func.avg(deepest.c.pct), func.count()).group_by(deepest.c.story_id)
    ).all():
        row = out.setdefault(int(sid), {"views": 0, "dwell": 0.0, "dwellN": 0, "clicks": 0, "depth": None, "depthN": 0})
        row["depth"] = round(float(avg or 0))
        row["depthN"] = int(reads or 0)
    return out


def visitor_pairs(conn, start, end) -> list[tuple]:
    """Yesterday's visitors as distinct (referrer, time zone, visitor) rows: about one row per
    visitor, so each is counted once per source and once per country."""
    ev = db.events.c
    who = func.coalesce(ev.visitor, ev.session)
    return [tuple(r) for r in conn.execute(
        select(ev.source, ev.tz, who).distinct()
        .where(ev.created_at >= start, ev.created_at < end, ev.type == "view")
    ).all()]


def readers_facts(events: dict[int, dict], stories: list[dict]) -> dict:
    """Readers against the ranking. The ranking's prediction is the front page's order: stories by
    score, as the site sorts them."""
    ranked = sorted(stories, key=lambda s: (-(s.get("score") or 0), s["id"]))
    rank = {s["id"]: i + 1 for i, s in enumerate(ranked)}
    by_id = {s["id"]: s for s in stories}
    rows = []
    for sid, e in events.items():
        if sid not in by_id:
            continue
        s = by_id[sid]
        rows.append({"id": sid, "headline": s.get("headline"), "slug": s.get("slug"), "rank": rank[sid],
                     "views": e["views"], "clicks": e["clicks"],
                     "dwell": round(e["dwell"] / e["dwellN"]) if e["dwellN"] else None,
                     "depth": e["depth"], "depthReads": e["depthN"]})
    viewed = sorted((r for r in rows if r["views"] > 0), key=lambda r: (-r["views"], r["rank"]))
    first = ranked[0] if ranked else None
    first_row = next((r for r in rows if first and r["id"] == first["id"]), None)
    return {
        "views": sum(r["views"] for r in viewed), "stories": len(viewed), "ranked": len(ranked),
        "top": viewed[:5],
        "first": {"headline": first.get("headline"), "views": first_row["views"] if first_row else 0} if first else None,
        "rows": viewed,
    }


def weights_now(admin: dict) -> dict[str, dict]:
    return {s["key"]: {"name": s["name"], "w": float(s.get("performance") or 0.0)}
            for s in admin.get("sources") or [] if s.get("enabled") and not s.get("discovered")}


def sources_facts(admin: dict, prev: dict | None) -> dict:
    """The learned weight (sources.engagement_ema, rank.py: 1.00 is the average source) against the
    previous note's copy of it."""
    cur = weights_now(admin)
    out = {"since": (prev or {}).get("day"), "mover": None, "max": None, "min": None, "any": any(v["w"] for v in cur.values())}
    if cur:
        hi = max(cur.items(), key=lambda kv: (kv[1]["w"], kv[0]))
        lo = min(cur.items(), key=lambda kv: (kv[1]["w"], kv[0]))
        out["max"] = {"name": hi[1]["name"], "w": round(hi[1]["w"], 2)}
        out["min"] = {"name": lo[1]["name"], "w": round(lo[1]["w"], 2)}
    before = (prev or {}).get("weights") or {}
    moves = [(k, before[k], v["w"], v["name"]) for k, v in cur.items() if k in before]
    if moves:
        k, was, now_w, name = max(moves, key=lambda m: (abs(round(m[2], 2) - round(m[1], 2)), m[0]))
        out["mover"] = {"name": name, "from": round(was, 2), "to": round(now_w, 2)}
    return out


def budget_facts(admin: dict, now: datetime) -> dict:
    d = admin.get("database") or {}
    reads, cycle = d.get("reads") or {}, d.get("cycle") or {}
    since = now - timedelta(hours=24)
    kb, runs = 0.0, set()
    for r in admin.get("runs") or []:
        st = r.get("stats") or {}
        try:
            at = datetime.fromisoformat(str(r.get("startedAt")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at >= since and st.get("readKB") is not None:
            kb += float(st["readKB"])
            if r.get("step") == "fetch":
                runs.add(r.get("startedAt"))
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    budgets = (admin.get("llm") or {}).get("budgets") or {}
    out_of = [{"provider": u["provider"], "requests": u["requests"], "budget": budgets.get(u["provider"])}
              for u in (admin.get("llm") or {}).get("usage") or []
              if u.get("day") == yesterday and u.get("exhausted") and u.get("provider") in budgets]
    # Mistral is paid by the token against a monthly cap: named only once it has spent 80% of it.
    m = (admin.get("llm") or {}).get("mistral") or {}
    mistral = None
    if m.get("capUSD") and (m.get("spentUSD") or 0) >= MISTRAL_NOTE_SHARE * m["capUSD"]:
        mistral = {"spentUSD": round(float(m["spentUSD"]), 2), "capUSD": round(float(m["capUSD"]), 2)}
    return {"readMB": round(kb / 1024, 1) if kb else None, "runs": len(runs),
            "monthlyMB": reads.get("monthlyMB"), "quotaMB": reads.get("quotaMB") or config.SUPABASE_EGRESS_GB * 1024,
            "projectedMB": cycle.get("projectedMB"), "measuredMB": cycle.get("measuredMB"), "cycleEnd": cycle.get("end"),
            "exhausted": out_of, "mistral": mistral}


def growth_facts(admin: dict, pairs: list[tuple], now: datetime) -> dict:
    from .admin import country_name, country_of, source_name

    per_day = {r["day"]: r for r in (admin.get("engagement") or {}).get("perDay") or []}
    y = (now.date() - timedelta(days=1)).isoformat()
    b = (now.date() - timedelta(days=2)).isoformat()
    groups: dict[str, set] = {}
    countries: dict[str, set] = {}
    for raw, tz, who in pairs:
        name = source_name(raw)
        name = "search" if name in SEARCH_ENGINES else "direct" if name == "Direct" else name
        groups.setdefault(name, set()).add(who)
        code = country_of(tz)
        if code:  # "Britain (UK)" reads as "Britain" in a sentence that puts counts in brackets
            countries.setdefault(re.sub(r"\s*\(.*?\)", "", country_name(code)), set()).add(who)
    return {
        "day": y, "before": b,
        "visitors": (per_day.get(y) or {}).get("visitors"), "visitorsBefore": (per_day.get(b) or {}).get("visitors"),
        "sources": [{"name": k, "visitors": len(v)} for k, v in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))][:3],
        "countries": [{"name": k, "visitors": len(v)} for k, v in sorted(countries.items(), key=lambda kv: (-len(kv[1]), kv[0]))][:2],
    }


def decide_facts(admin: dict, stories: list[dict], readers: dict, budget: dict, now: datetime) -> dict:
    """Everything that is off, most pressing first; the note asks about the first one."""
    out: list[dict] = []
    runs_3d = FAILING_DAYS * config.RUNS_PER_DAY
    failing = sorted((s for s in admin.get("sources") or []
                      if s.get("enabled") and not s.get("discovered") and (s.get("errorCount") or 0) >= runs_3d),
                     key=lambda s: -(s.get("errorCount") or 0))
    if failing:
        s = failing[0]
        out.append({"kind": "feed", "name": s["name"], "runs": s["errorCount"], "others": len(failing) - 1})
    usage = (admin.get("llm") or {}).get("usage") or []
    budgets = (admin.get("llm") or {}).get("budgets") or {}
    last3 = {(now.date() - timedelta(days=i)).isoformat() for i in range(1, FAILING_DAYS + 1)}
    for p in budgets:
        days_out = {u["day"] for u in usage if u.get("provider") == p and u.get("exhausted") and u.get("day") in last3}
        if len(days_out) == FAILING_DAYS:
            out.append({"kind": "provider", "provider": p, "budget": budgets[p]})
            break
    quota, projected = budget.get("quotaMB"), budget.get("projectedMB")
    if projected and quota and projected > quota:
        out.append({"kind": "egress", "projectedMB": projected, "quotaMB": quota})
    shallow = [r for r in readers.get("rows") or [] if r["views"] >= LOW_DEPTH_VIEWS and r["depth"] is not None and r["depth"] < LOW_DEPTH_PERCENT]
    if shallow:
        r = max(shallow, key=lambda r: (r["views"], -r["depth"]))
        out.append({"kind": "depth", "headline": r["headline"], "views": r["views"], "depth": r["depth"]})
    week_ago = _iso(now - timedelta(days=7))
    recent = Counter(s.get("category") for s in stories if (s.get("firstPublishedAt") or "") >= week_ago)
    empty = [k for k in config.CATEGORIES if not recent.get(k)]
    if stories and empty:
        focus = config.BRIEFING_FOCUS_CATEGORY
        k = focus if focus in empty else empty[0]
        out.append({"kind": "category", "name": config.CATEGORIES[k]})
    missing = [m for m in ((admin.get("engagement") or {}).get("searches7") or {}).get("missing") or [] if m.get("visitors", 0) >= 2]
    if missing:
        out.append({"kind": "search", "query": missing[0]["query"], "visitors": missing[0]["visitors"]})
    return {"issues": out}


# ---------------------------------------------------------------------------- the sentences

def s_briefing(b: dict | None) -> str:
    if not b:
        return "The briefing had no lead story this morning: nothing was new enough to go in it."
    pubs = plural(b["publishers"], "publisher")
    primary = f"and the primary source ({b['primary']})" if b["primary"] else ("and the primary source" if b["hasPrimary"] else "but not the primary source")
    if b["pinned"]:
        why = "because it is pinned"
    elif b["passed"]:
        why = (f"because confirmed stories go first: it moved above {_q(b['passed']['headline'])} "
               f"(score {b['passed']['score']:.3f}), which only one publisher had reported")
    elif b["highest"]:
        why = "on the highest score in the briefing"
    else:
        why = "on its place in the ranking"
    return f"{_q(b['headline'])} led the briefing {why}; it scored {b['score']:.3f}, with {pubs} {primary}."


def s_readers(r: dict) -> str:
    if not r["views"]:
        return "No reader opened a story page in the last 24 hours, so there is nothing yet to set against the ranking."
    top = r["top"][0]
    how = [plural(top["views"], "view")]
    if top["depth"] is not None:
        how.append(f"{top['depth']}% average read depth")
    elif top["dwell"] is not None:
        how.append(f"{top['dwell']} seconds on the page")
    if top["clicks"]:
        how.append(plural(top["clicks"], "click") + " to the source")
    head = f"Readers opened {_q(top['headline'])} most ({', '.join(how)})"
    first = r["first"]
    if top["rank"] > 5:
        tail = f", the biggest surprise, as the ranking had it at #{top['rank']}"
        if first and first["headline"] != top["headline"]:
            tail += f" while its first choice, {_q(first['headline'])}, drew {plural(first['views'], 'view')}"
        return head + tail + "."
    if first and first["headline"] != top["headline"] and first["views"] * 2 < top["views"]:
        return head + (f" at #{top['rank']} in the ranking; the biggest surprise is that its first choice, "
                       f"{_q(first['headline'])}, drew " + ("no views." if not first["views"] else f"only {plural(first['views'], 'view')}."))
    gaps = [x for x in r["rows"][1:] if x["rank"] - (r["rows"].index(x) + 1) >= 5]
    if gaps:
        g = max(gaps, key=lambda x: (x["rank"] - (r["rows"].index(x) + 1), x["views"]))
        return head + (f", which the ranking had at #{top['rank']}; the biggest surprise was {_q(g['headline'])}, "
                       f"ranked #{g['rank']} but opened {plural(g['views'], 'time')}.")
    return head + f", which the ranking had at #{top['rank']}, so readers and the ranking broadly agreed."


def s_sources(s: dict) -> str:
    m = s["mover"]
    if m and round(m["to"] - m["from"], 2) != 0:
        verb, direction = ("earned", "rose") if m["to"] > m["from"] else ("lost", "fell")
        return (f"{m['name']} {verb} the most weight since the note of {short_day(s['since'])}: its learned score "
                f"{direction} from {m['from']:.2f} to {m['to']:.2f}, where 1.00 is an average source.")
    if not s["any"]:
        return "No source has learned any weight from readers or the web yet."
    if m:
        return (f"No source's learned weight moved since the note of {short_day(s['since'])}; {s['max']['name']} "
                f"still carries the most ({s['max']['w']:.2f}, where 1.00 is an average source).")
    return (f"There is no earlier note to compare with; {s['max']['name']} carries the most learned weight "
            f"({s['max']['w']:.2f}) and {s['min']['name']} the least ({s['min']['w']:.2f}), where 1.00 is an average source.")


def work_clause(c: dict | None) -> str:
    """AI at Work, only when reader data changed its featured pick or a top-three place
    (work_learn.changed): a clause for the sources sentence, which is about what readers taught."""
    if not c or not c.get("to"):
        return ""
    if c["kind"] == "featured":
        instead = f" instead of {_q(c['from'])}" if c.get("from") else ""
        return f"; on AI at Work, reader data made {_q(c['to'])} the featured pick{instead}"
    was = f" (the rules alone had it at #{c['was']})" if c.get("was") else ""
    return f"; on AI at Work, reader data put {_q(c['to'])} at #{c['at']}{was}"


def with_work(sentence: str, c: dict | None) -> str:
    clause = work_clause(c)
    return sentence[:-1] + clause + "." if clause and sentence.endswith(".") else sentence


PROVIDER_NAMES = {"gemini": "Gemini", "groq": "Groq", "cloud": "Ollama Cloud", "mistral": "Mistral"}
MISTRAL_NOTE_SHARE = 0.8  # the budget sentence names Mistral from this share of its monthly cap


def mistral_clause(m: dict | None) -> str:
    if not m:
        return ""
    if m["spentUSD"] >= m["capUSD"] - 0.01:  # calls stop a call's cost short of the cap
        return f"; Mistral has spent its ${m['capUSD']:.2f} monthly cap and stays off until the 1st"
    return f"; Mistral has spent ${m['spentUSD']:.2f} of its ${m['capUSD']:.2f} monthly cap"


def s_budget(b: dict) -> str:
    """The budget sentence, with Mistral's spend added as a clause once it passes 80% of its cap."""
    sentence = _s_budget(b)
    clause = mistral_clause(b.get("mistral"))
    return sentence[:-1] + clause + "." if clause and sentence.endswith(".") else sentence


def _s_budget(b: dict) -> str:
    quota, projected = b["quotaMB"], b.get("projectedMB")
    if projected and quota and projected > quota:
        return (f"Database reads are on course for {mb(projected)} MB this billing cycle, over the {n(quota)} MB "
                f"free allowance, with {mb(b.get('measuredMB') or 0)} MB used so far.")
    if b["exhausted"]:
        e = b["exhausted"][0]
        name = PROVIDER_NAMES.get(e["provider"], e["provider"])
        if e["requests"] >= e["budget"]:
            return (f"{name} used all of its free allowance yesterday ({n(e['requests'])} of {n(e['budget'])} requests), "
                    "so later summaries went to the other models.")
        # Stopped by the provider's own limit before our daily budget ran out (a 429 or a quota error).
        return (f"{name} hit its provider's limit yesterday after {plural(e['requests'], 'request')} of the {n(e['budget'])} "
                "the pipeline allows it, so later summaries went to the other models.")
    if b["readMB"] is not None:
        tail = f", on course for {mb(b['monthlyMB'])} MB a month" if b.get("monthlyMB") is not None else ""
        return (f"The pipeline read {mb(b['readMB'])} MB from the database in the last 24 hours "
                f"({plural(b['runs'], 'run')}){tail} against the {n(quota)} MB free allowance.")
    return "No database reads were recorded in the last 24 hours, so there is no budget figure to report."


def _the(country: str) -> str:
    plural_or_union = country.startswith(("United ", "Czech Republic", "Dominican Republic", "Central African")) or country.endswith(("lands", "pines", "Islands", "Bahamas", "Gambia", "Emirates"))
    return f"the {country}" if plural_or_union else country


def s_growth(g: dict) -> str:
    y, b = g["visitors"], g["visitorsBefore"]
    if not y and not b:
        return f"No visitors were recorded on {short_day(g['day'])} or the day before."
    y, b = y or 0, b or 0
    if y > b:
        head = f"Visitors rose to {n(y)} on {short_day(g['day'])} from {n(b)} the day before"
    elif y < b:
        head = f"Visitors fell to {n(y)} on {short_day(g['day'])} from {n(b)} the day before"
    else:
        head = f"Visitors held at {n(y)} on {short_day(g['day'])}, the same as the day before"
    parts = []
    for s in g["sources"]:
        k = s["visitors"]
        if s["name"] == "direct":
            parts.append(f"{n(k)} came direct")
        elif s["name"] == "search":
            parts.append(f"{n(k)} from search")
        else:
            parts.append(f"{n(k)} from {s['name']}")
    tail = ""
    if parts:
        tail = "; " + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1])
    if g["countries"]:
        where = " and ".join(f"{_the(x['name'])} ({n(x['visitors'])})" for x in g["countries"])
        tail += f"; most were in {where}"
    return head + tail + "."


def s_decide(d: dict) -> str:
    if not d["issues"]:
        return "Nothing is far enough off to need a decision today."
    i = d["issues"][0]
    k = i["kind"]
    if k == "feed":
        more = f" ({plural(i['others'], 'other feed')} also failing)" if i["others"] else ""
        return (f"Should {i['name']} be paused or given a new feed address? It has failed on each of its last "
                f"{n(i['runs'])} runs, {FAILING_DAYS} days or more{more}.")
    if k == "provider":
        name = PROVIDER_NAMES.get(i["provider"], i["provider"])
        return (f"Should another free model key be added, or fewer articles summarised each run? {name} ran out "
                f"of its free allowance on each of the last {FAILING_DAYS} days.")
    if k == "egress":
        return (f"Should the pipeline run less often (RUNS_PER_DAY and the schedule)? Database reads are on course for "
                f"{mb(i['projectedMB'])} MB this cycle against {n(i['quotaMB'])} MB.")
    if k == "depth":
        return (f"Should the summary of {_q(i['headline'])} be rewritten? It drew {plural(i['views'], 'view')}, but "
                f"readers got on average only {i['depth']}% of the way down the page.")
    if k == "category":
        return f"Should {i['name']} get more feeds, or leave the menu? It has had no new story in the last 7 days."
    if k == "search":
        return (f"Should the site cover {_q(i['query'])}? {plural(i['visitors'], 'visitor')} searched for it in the "
                "last 7 days and found nothing.")
    return "Nothing is far enough off to need a decision today."


def rules_sentences(f: dict) -> list[str]:
    return [s_briefing(f["briefing"]), s_readers(f["readers"]), with_work(s_sources(f["sources"]), f.get("work")),
            s_budget(f["budget"]), s_growth(f["growth"]), s_decide(f["decide"])]


# ---------------------------------------------------------------------------- polishing, fact-safe

POLISH_PROMPT = """You edit a six-sentence morning note that a news site's pipeline writes for its owner.
Make each sentence read smoothly in plain, calm British English. No hype, no exclamation marks, no emoji.
Rules you must keep:
- Keep six sentences, in the same order, one idea each.
- Keep every number exactly as written (same digits, same units, same % and #). Do not add, drop,
  round or spell out any number.
- Keep every name and every quoted headline exactly as written. Do not add names.
- The last sentence stays a question to the owner.
Sentences:
{sentences}
Return ONLY JSON: {{"sentences": ["...", "...", "...", "...", "...", "..."]}}"""

_CAPS = re.compile(r"\b[A-Z][A-Za-z0-9&'.-]*[A-Za-z0-9]|\b[A-Z]\b")
CONNECTIVES = {"meanwhile", "yesterday", "today", "overall", "also", "however", "that", "this", "it", "there", "the",
               "an", "in", "on", "by", "with", "as", "its", "their", "while", "since", "so", "and", "but", "still",
               "over", "at", "for", "from", "most", "nothing", "no", "one", "should", "would", "is", "was"}
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿]")


def figures(text: str) -> Counter:
    """Every figure written with digits, by value; "#3" and "3" are the same figure."""
    return Counter(v for w, v in numbers_in(text or "") if any(ch.isdigit() for ch in w))


def polish_problem(rules: list[str], polished) -> str | None:
    """Why a reworded note cannot replace the rules text, or None when it can. Every sentence must
    carry exactly its rules sentence's figures (no more, no fewer, none rounded) and name nothing
    the rules sentence does not."""
    if not isinstance(polished, list) or len(polished) != len(rules) or not all(isinstance(p, str) and p.strip() for p in polished):
        return "not six sentences"
    for i, (r, p) in enumerate(zip(rules, polished), 1):
        if figures(r) != figures(p):
            extra = sorted((figures(p) - figures(r)).elements())
            gone = sorted((figures(r) - figures(p)).elements())
            return f"sentence {i}: figures changed (added {extra}, lost {gone})"
        # Every capitalised word is a possible name and must be in the rules sentence (checks.py
        # compares case-blind, so "Readers" is covered by "readers"); a plain connective that starts
        # a clause is grammar, not a name.
        starts = {m.group(1) for m in re.finditer(r"(?:^|[.;:?,]\s+)([A-Z][A-Za-z'-]*)", p)}
        names = [w for w in _CAPS.findall(p) if not (w in starts and w.lower() in CONNECTIVES)]
        bad = checks.unsupported_names(names, p, r)
        if bad:
            return f"sentence {i}: names not in the facts ({', '.join(bad[:3])})"
        if _EMOJI.search(p) or "!" in p:
            return f"sentence {i}: emoji or exclamation"
        if len(p) > 1.6 * len(r) + 40:
            return f"sentence {i}: much longer than the facts"
    if not polished[-1].rstrip().endswith(("?", ".")):
        return "last sentence cut off"
    return None


def polish(rules: list[str], call=None) -> tuple[list[str], dict]:
    """The note reworded by a model when that is safe; the rules text otherwise. `call` takes a
    prompt and returns the parsed JSON (enrich.call_groq by default)."""
    info: dict = {"polished": False}
    if os.environ.get("MORNING_NOTE_POLISH", "1") in ("0", "false", "no"):
        info["reason"] = "off"
        return rules, info
    prompt = POLISH_PROMPT.format(sentences="\n".join(f"{i}. {s}" for i, s in enumerate(rules, 1)))
    try:
        if call is None:
            call = _default_call(info)
            if call is None:
                return rules, info
        result = call(prompt)
        polished = [str(x).strip() for x in (result or {}).get("sentences") or []] if isinstance(result, dict) else None
    except Exception as exc:  # noqa: BLE001 - any failure keeps the rules text
        info["reason"] = f"model failed: {str(exc)[:80]}"
        return rules, info
    problem = polish_problem(rules, polished)
    if problem:
        info["reason"] = problem
        return rules, info
    info["polished"] = True
    return polished, info


def _default_call(info: dict):
    if not config.GROQ_API_KEY:
        info["reason"] = "no model key"
        return None
    from . import enrich

    eng = db.engine()
    with eng.connect() as conn:
        if enrich.allowance(conn, "groq") <= 0:
            info["reason"] = "no allowance left"
            return None

    def call(prompt: str) -> dict:
        old = enrich._deadline
        enrich._deadline = time.monotonic() + 60
        try:
            result = enrich.call_groq(prompt)
        finally:
            enrich._deadline = old
        enrich.record_usage(eng, "groq", 1)
        info["provider"] = "groq"
        return result

    return call


# ---------------------------------------------------------------------------- building and storing

def build(admin: dict, briefing: dict | None, stories: list[dict], events: dict[int, dict], pairs: list[tuple],
          prev: dict | None, now: datetime, call=None, work_briefing: dict | None = None) -> dict:
    """The note for `now` from data already in hand. Pure apart from the optional model call.
    `work_briefing` (work-briefing.json) adds AI at Work to the sources sentence only when reader
    data changed its featured pick or a top-three place."""
    stories = [s for s in stories if s.get("id") is not None]
    readers = readers_facts(events, stories)
    budget = budget_facts(admin, now)
    facts = {
        "briefing": briefing_facts(briefing, stories),
        "readers": readers,
        "sources": sources_facts(admin, prev),
        "budget": budget,
        "growth": growth_facts(admin, pairs, now),
        "work": ((work_briefing or {}).get("learning") or {}).get("changed"),
    }
    facts["decide"] = decide_facts(admin, stories, readers, budget, now)
    rules = rules_sentences(facts)
    text, info = polish(rules, call)
    day = now.date().isoformat()
    facts["readers"] = {k: v for k, v in readers.items() if k != "rows"}
    return {
        "day": day, "title": title_for(day), "writtenAt": _iso(now),
        "sentences": [{"key": k, "label": label, "text": t} for (k, label), t in zip(LABELS, text)],
        "rules": rules, "polish": info,
        "weights": {k: round(v["w"], 4) for k, v in weights_now(admin).items()},
        "facts": facts,
    }


def store_path():
    return config.CACHE_DIR / "morning" / "notes.json"


def load_notes() -> list[dict] | None:
    """The kept notes, newest first; None when the runner's copy is missing."""
    p = store_path()
    if not p.exists():
        return None
    data = _read(p, {})
    return list(data.get("notes") or [])


def save_notes(notes: list[dict]) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"notes": notes[:KEEP]}, ensure_ascii=False), encoding="utf-8")


def export(notes: list[dict]) -> None:
    """site/src/data/morning.json for the admin page: the notes without the comparison data."""
    public = [{k: v for k, v in x.items() if k not in ("weights", "facts")} for x in notes[:KEEP]]
    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / "morning.json").write_text(json.dumps({"notes": public}, ensure_ascii=False), encoding="utf-8")


def embed(note: dict) -> str:
    """The note as a hidden comment for its GitHub issue, so a lost runner cache can be rebuilt."""
    raw = json.dumps({k: v for k, v in note.items() if k != "facts"}, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- {NOTE_MARK}:{base64.b64encode(raw.encode()).decode()} -->"


def unembed(body: str | None) -> dict | None:
    m = re.search(rf"<!-- {NOTE_MARK}:([A-Za-z0-9+/=]+) -->", body or "")
    if not m:
        return None
    try:
        return json.loads(base64.b64decode(m.group(1)).decode())
    except (ValueError, UnicodeDecodeError):
        return None


def run(now: datetime | None = None) -> dict:
    now = now or db.utcnow()
    stats: dict = {"written": False}
    notes = load_notes()
    if notes is None:
        from . import notify

        notes = notify.recover_morning_notes()
        stats["recovered"] = len(notes)
    notes = sorted(notes, key=lambda x: x.get("day") or "", reverse=True)[:KEEP]
    if due(now, notes):
        admin = _read(config.SITE_DATA_DIR / "admin.json", None)
        if admin is None:
            stats["reason"] = "no admin.json"
        else:
            briefing = _read(config.SITE_DATA_DIR / "briefing.json", None)
            stories = _read(config.SITE_DATA_DIR / "stories.json", [])
            y0 = datetime.combine(now.date() - timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
            with db.engine().connect() as conn:
                events = reader_events(conn, now - timedelta(hours=24))
                pairs = visitor_pairs(conn, y0, y0 + timedelta(days=1))
            work_briefing = _read(config.SITE_DATA_DIR / "work-briefing.json", None)
            note = build(admin, briefing, stories, events, pairs, notes[0] if notes else None, now,
                         work_briefing=work_briefing)
            notes = [note] + [x for x in notes if x.get("day") != note["day"]]
            stats.update(written=True, day=note["day"], polished=note["polish"]["polished"],
                         reason=note["polish"].get("reason"))
            log.info("morning note %s: %s", note["day"], " ".join(s["text"] for s in note["sentences"]))
    else:
        stats["reason"] = "already written today" if any(x.get("day") == now.date().isoformat() for x in notes) else f"before {HOUR_UTC:02d}:00 UTC"
    save_notes(notes)
    export(notes)
    stats["kept"] = len(notes[:KEEP])
    return stats
