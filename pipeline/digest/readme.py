"""Step: a short "Today in AI" section at the top of the repository's README.

The repository is public, and GitHub pages are read by search engines and AI assistants that do not
index digestai.news itself yet (Bing had none of the site on 13 Oct 2026, while it ranked the GitHub
organisation page for the name). Once a day the README carries today's briefing: the five stories,
each linked to its page on the site with the number of sources it merges. The section sits between
two markers, so the rest of the README is never touched.

One commit a day through the contents API, after FIRST_HOUR_UTC, skipped when the section already
shows today's date. Nothing is read from the database: the export's briefing.json and stories.json
are the input. Without a token (local runs) the step does nothing.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone

import requests

from . import config

log = logging.getLogger("digest.readme")

API = "https://api.github.com"
PATH = "README.md"
MARK_START = "<!-- today-in-ai:start -->"
MARK_END = "<!-- today-in-ai:end -->"
FIRST_HOUR_UTC = 6  # the briefing has settled on the day's news by then
STORIES = 5


def day_label(now: datetime) -> str:
    return f"{now.day} {now.strftime('%B %Y')}"


def section(stories: list[dict], now: datetime, site: str) -> str:
    """The section between the markers: today's date, the stories with their source counts, links."""
    lines = [MARK_START, f"## Today in AI, {day_label(now)}", "",
             f"The stories that matter today, each merged from every outlet that reported it. "
             f"Read them with all their sources at **[digestai.news/today]({site}/today)**.", ""]
    for i, s in enumerate(stories[:STORIES], 1):
        n = int(s.get("articleCount") or 1)
        lines.append(f"{i}. [{s['headline']}]({site}/story/{s['slug']}) ({n} source{'s' if n != 1 else ''})")
    lines += ["", f"More: [AI at Work]({site}/work) · [Model releases]({site}/models) · "
                  f"[Funding]({site}/funding) · [Daily audio briefing]({site}/listen)", MARK_END]
    return "\n".join(lines)


def splice(readme: str, block: str) -> str:
    """Replace the marked section, or put it after the first heading when there is none yet."""
    if MARK_START in readme and MARK_END in readme:
        head, rest = readme.split(MARK_START, 1)
        _, tail = rest.split(MARK_END, 1)
        return head + block + tail
    lines = readme.split("\n")
    at = next((i + 1 for i, line in enumerate(lines) if line.startswith("# ")), 0)
    return "\n".join(lines[:at] + ["", block, ""] + lines[at:])


def briefing_stories() -> list[dict]:
    data = config.SITE_DATA_DIR
    briefing = json.loads((data / "briefing.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in json.loads((data / "stories.json").read_text(encoding="utf-8"))}
    return [by_id[i] for i in briefing.get("storyIds") or [] if i in by_id]


def run(session=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if session is None and (not token or not repo):
        return {"skipped": "no token"}
    if now.hour < FIRST_HOUR_UTC:
        return {"skipped": "before the day's briefing settles"}
    try:
        stories = briefing_stories()
    except (OSError, ValueError, KeyError) as exc:
        return {"skipped": f"no briefing ({exc})"}
    if not stories:
        return {"skipped": "empty briefing"}
    s = session or requests.Session()
    if session is None:
        s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    repo = repo or "Optimumedia/digestai"
    r = s.get(f"{API}/repos/{repo}/contents/{PATH}", timeout=30)
    if r.status_code != 200:
        log.warning("README not readable: HTTP %s", r.status_code)
        return {"skipped": f"read HTTP {r.status_code}"}
    current = r.json()
    readme = base64.b64decode(current.get("content") or "").decode("utf-8")
    if f"## Today in AI, {day_label(now)}" in readme:
        return {"skipped": "already today"}
    updated = splice(readme, section(stories, now, config.SITE_URL.rstrip("/")))
    w = s.put(f"{API}/repos/{repo}/contents/{PATH}", json={
        "message": f"README: today in AI, {now.day} {now.strftime('%b %Y')}",
        "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
        "sha": current.get("sha"),
    }, timeout=30)
    if w.status_code not in (200, 201):
        log.warning("README not updated: HTTP %s %s", w.status_code, w.text[:120])
        return {"skipped": f"write HTTP {w.status_code}"}
    return {"updated": True, "stories": min(len(stories), STORIES)}
