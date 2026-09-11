"""Step: turn dashboard alerts into a GitHub issue so someone actually hears about them.

One issue labelled "needs-attention" is kept open while critical alerts exist, its body
refreshed when the alert list changes, and closed with a comment when everything is clear.
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
