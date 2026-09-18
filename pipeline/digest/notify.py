"""Step: turn dashboard alerts into a GitHub issue so someone actually hears about them.

One issue labelled "needs-attention" is kept open while critical alerts exist, its body
refreshed when the alert list changes, and closed with a comment when everything is clear.
The morning note (morning.py) gets one issue a day, labelled "morning-note" and titled
"Morning note, 19 Sep 2026"; opening it closes the previous day's, so only one is open.
GitHub then delivers the notification by email or app, no extra service needed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

import requests

from . import config

log = logging.getLogger("digest.notify")

LABEL = "needs-attention"
TITLE = "Pipeline needs attention"


def _api():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None, None
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    return s, f"https://api.github.com/repos/{repo}"


def _body(alerts: list[dict], generated_at: str) -> str:
    crit = [a for a in alerts if a["level"] == "critical"]
    warn = [a for a in alerts if a["level"] != "critical"]
    lines = [f"Updated automatically by the pipeline at {generated_at}. Dashboard: {config.SITE_URL}/admin", ""]
    if crit:
        lines += ["## Critical", *[f"- {a['text']}" for a in crit], ""]
    if warn:
        lines += ["## Warnings", *[f"- {a['text']}" for a in warn], ""]
    lines.append("This issue closes itself when the critical alerts clear.")
    body = "\n".join(lines)
    return body + f"\n\n<!-- digest-alerts:{hashlib.sha1(json.dumps(alerts, sort_keys=True).encode()).hexdigest()[:12]} -->"


def run() -> dict:
    stats = alerts_issue()
    try:
        stats["morning"] = morning_issue()
    except Exception as exc:  # noqa: BLE001 - the morning note's issue must never cost the alerts
        log.warning("morning note issue failed: %s", str(exc)[:200])
        stats["morning"] = {"action": "failed", "error": str(exc)[:120]}
    return stats


def alerts_issue() -> dict:
    stats = {"issue": None, "action": "none"}
    admin_file = config.SITE_DATA_DIR / "admin.json"
    if not admin_file.exists():
        return stats
    admin = json.loads(admin_file.read_text(encoding="utf-8"))
    alerts = admin.get("alerts") or []
    critical = [a for a in alerts if a["level"] == "critical"]
    s, base = _api()
    if s is None:
        stats["action"] = "no token"
        return stats

    r = s.get(f"{base}/issues", params={"labels": LABEL, "state": "open", "per_page": 5}, timeout=20)
    r.raise_for_status()
    open_issues = [i for i in r.json() if "pull_request" not in i]
    issue = open_issues[0] if open_issues else None
    body = _body(alerts, admin.get("generatedAt", ""))

    if critical:
        if issue is None:
            s.post(f"{base}/labels", json={"name": LABEL, "color": "d03b3b", "description": "Opened by the pipeline when something needs a human"}, timeout=20)
            r = s.post(f"{base}/issues", json={"title": TITLE, "body": body, "labels": [LABEL]}, timeout=20)
            r.raise_for_status()
            stats.update(issue=r.json()["number"], action="opened")
        elif body.split("<!-- digest-alerts:")[1] != (issue.get("body") or "").split("<!-- digest-alerts:")[-1]:
            s.patch(f"{base}/issues/{issue['number']}", json={"body": body}, timeout=20).raise_for_status()
            stats.update(issue=issue["number"], action="updated")
        else:
            stats.update(issue=issue["number"], action="unchanged")
    elif issue is not None:
        s.post(f"{base}/issues/{issue['number']}/comments", json={"body": "All critical alerts have cleared. Closing automatically."}, timeout=20)
        s.patch(f"{base}/issues/{issue['number']}", json={"state": "closed", "state_reason": "completed"}, timeout=20).raise_for_status()
        stats.update(issue=issue["number"], action="closed")
    return stats


# ---------------------------------------------------------------------------- the morning note

NOTE_LABEL = "morning-note"


def note_body(note: dict) -> str:
    from . import morning

    mention = (os.environ.get("MORNING_NOTE_MENTION") or "").strip().lstrip("@")
    written = (note.get("writtenAt") or "")[11:16]
    lines = [f"Written by the pipeline at {written} UTC about the last 24 hours. Dashboard: {config.SITE_URL}/admin", ""]
    for s in note.get("sentences") or []:
        lines += [f"**{s['label']}.** {s['text']}", ""]
    if (note.get("polish") or {}).get("polished"):
        lines.append("_Reworded by a model and checked against the rules text: every figure and name is the data's._")
    else:
        lines.append("_Written from the data by rules only._")
    if mention:
        lines += ["", f"@{mention}"]
    lines += ["", "The previous note closes itself when this one opens.", "", morning.embed(note)]
    return "\n".join(lines)


def morning_issue(now=None) -> dict:
    """Open today's morning-note issue if it does not exist yet (open or closed: a note the owner
    closed is not opened again), then close the older open ones."""
    from . import db, morning

    now = now or db.utcnow()
    note = next((x for x in morning.load_notes() or [] if x.get("day") == now.date().isoformat()), None)
    if note is None:
        return {"action": "no note today"}
    s, base = _api()
    if s is None:
        return {"action": "no token"}
    r = s.get(f"{base}/issues", params={"labels": NOTE_LABEL, "state": "all", "per_page": 10, "sort": "created", "direction": "desc"}, timeout=20)
    r.raise_for_status()
    issues = [i for i in r.json() if "pull_request" not in i]
    title = morning.title_for(note["day"])
    today = next((i for i in issues if i.get("title") == title), None)
    out = {"action": "exists", "issue": today["number"]} if today else {}
    if today is None:
        s.post(f"{base}/labels", json={"name": NOTE_LABEL, "color": "5b7083", "description": "The pipeline's daily morning note"}, timeout=20)
        r = s.post(f"{base}/issues", json={"title": title, "body": note_body(note), "labels": [NOTE_LABEL]}, timeout=20)
        r.raise_for_status()
        today = r.json()
        out = {"action": "opened", "issue": today["number"]}
    closed = []
    for i in issues:
        if i.get("state") == "open" and i["number"] != today["number"]:
            s.patch(f"{base}/issues/{i['number']}", json={"state": "closed", "state_reason": "completed"}, timeout=20).raise_for_status()
            closed.append(i["number"])
    if closed:
        out["closed"] = closed
    return out


def recover_morning_notes() -> list[dict]:
    """The kept notes rebuilt from their issues, for a run whose cache copy is missing. Empty when
    there is no token or GitHub does not answer: today's note is then simply written afresh, and
    morning_issue() still will not open a second issue for a day that has one."""
    from . import morning

    s, base = _api()
    if s is None:
        return []
    try:
        r = s.get(f"{base}/issues", params={"labels": NOTE_LABEL, "state": "all", "per_page": morning.KEEP, "sort": "created", "direction": "desc"}, timeout=20)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read earlier morning notes: %s", str(exc)[:160])
        return []
    notes = [morning.unembed(i.get("body")) for i in r.json() if "pull_request" not in i]
    return [x for x in notes if x and x.get("day")]
