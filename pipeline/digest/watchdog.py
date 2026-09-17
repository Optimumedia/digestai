"""The pipeline's watchdog, run by .github/workflows/watchdog.yml on its own schedule.

The pipeline raises its own alerts (notify.py), which cannot help when it stops running at all:
a broken workflow file, Actions switched off, or GitHub disabling the schedule after 60 days
without repository activity. This checks, from outside the pipeline:

- when the last successful "Ingest and publish" run finished; older than MAX_AGE_HOURS opens one
  issue labelled "pipeline-stopped" (updated, never duplicated) and closes it once runs succeed;
- once a week (or when it is found disabled), re-enables the pipeline workflow and this one
  through the API. That counts as activity for the 60-day rule without a commit, so nothing is
  rebuilt or redeployed.

Only the standard library and requests; no database access.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import requests

API = "https://api.github.com"
PIPELINE = "pipeline.yml"
SELF = "watchdog.yml"
LABEL = "pipeline-stopped"
TITLE = "The pipeline has stopped publishing"
MAX_AGE_HOURS = float(os.environ.get("WATCHDOG_MAX_AGE_HOURS") or 3)
KEEPALIVE_WEEKDAY = 0  # Monday
KEEPALIVE_HOUR = 4


def session(token: str):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    return s


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def last_success(s, repo: str) -> dict | None:
    r = s.get(f"{API}/repos/{repo}/actions/workflows/{PIPELINE}/runs", params={"status": "success", "per_page": 1}, timeout=30)
    r.raise_for_status()
    runs = r.json().get("workflow_runs") or []
    return runs[0] if runs else None


def decide(last: dict | None, now: datetime, max_age_hours: float = MAX_AGE_HOURS) -> tuple[bool, float | None]:
    """(stopped, hours since the last successful run finished)."""
    if not last:
        return True, None
    at = _parse(last.get("updated_at") or last.get("run_started_at") or last.get("created_at"))
    hours = (now - at).total_seconds() / 3600
    return hours > max_age_hours, round(hours, 1)


def body(last: dict | None, hours: float | None, state: str, repo: str, now: datetime) -> str:
    lines = [f"Checked automatically at {now.strftime('%Y-%m-%d %H:%M')} UTC by the watchdog workflow.", ""]
    if last:
        lines.append(f"The last successful run finished {hours:.1f} hours ago: {last.get('html_url')}")
    else:
        lines.append("No successful run was found.")
    lines += ["", "While this lasts, no new stories appear on digestai.news.", "", "What to check:",
              f"- The Actions tab: https://github.com/{repo}/actions/workflows/{PIPELINE} (failed runs show the error).",
              f"- Whether the workflow is switched on (it is now: {state}).",
              "- Whether GitHub Actions is having an incident: https://www.githubstatus.com",
              "", "This issue closes itself after the next successful run."]
    return "\n".join(lines)


def workflow_state(s, repo: str, name: str) -> str:
    r = s.get(f"{API}/repos/{repo}/actions/workflows/{name}", timeout=30)
    return r.json().get("state", "unknown") if r.status_code == 200 else "unknown"


def keepalive(s, repo: str, now: datetime, states: dict[str, str], force: bool = False) -> list[str]:
    """Re-enable workflows that are disabled, and all of them once a week."""
    weekly = now.weekday() == KEEPALIVE_WEEKDAY and now.hour == KEEPALIVE_HOUR
    done = []
    for name in (PIPELINE, SELF):
        if force or weekly or states.get(name) not in ("active", "unknown"):
            r = s.put(f"{API}/repos/{repo}/actions/workflows/{name}/enable", timeout=30)
            if r.status_code in (200, 204):
                done.append(name)
    return done


def sync_issue(s, repo: str, stopped: bool, text: str) -> str:
    r = s.get(f"{API}/repos/{repo}/issues", params={"labels": LABEL, "state": "open", "per_page": 5}, timeout=30)
    r.raise_for_status()
    issues = [i for i in r.json() if "pull_request" not in i]
    issue = issues[0] if issues else None
    if stopped:
        if issue is None:
            s.post(f"{API}/repos/{repo}/labels", json={"name": LABEL, "color": "b60205", "description": "Opened by the watchdog when no run succeeds for hours"}, timeout=30)
            s.post(f"{API}/repos/{repo}/issues", json={"title": TITLE, "body": text, "labels": [LABEL]}, timeout=30).raise_for_status()
            return "opened"
        s.patch(f"{API}/repos/{repo}/issues/{issue['number']}", json={"body": text}, timeout=30).raise_for_status()
        return "updated"
    if issue is not None:
        s.post(f"{API}/repos/{repo}/issues/{issue['number']}/comments", json={"body": "A pipeline run succeeded again. Closing automatically."}, timeout=30)
        s.patch(f"{API}/repos/{repo}/issues/{issue['number']}", json={"state": "closed", "state_reason": "completed"}, timeout=30).raise_for_status()
        return "closed"
    return "none"


def run(s, repo: str, now: datetime | None = None, force_keepalive: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    states = {n: workflow_state(s, repo, n) for n in (PIPELINE, SELF)}
    enabled = keepalive(s, repo, now, states, force_keepalive)
    last = last_success(s, repo)
    stopped, hours = decide(last, now)
    state = "enabled" if states.get(PIPELINE) == "active" or PIPELINE in enabled else states.get(PIPELINE, "unknown")
    action = sync_issue(s, repo, stopped, body(last, hours, state, repo, now))
    return {"stopped": stopped, "hours": hours, "issue": action, "reenabled": enabled, "states": states}


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GH_TOKEN and GITHUB_REPOSITORY are needed")
        return 2
    result = run(session(token), repo, force_keepalive=os.environ.get("KEEPALIVE") == "1")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
