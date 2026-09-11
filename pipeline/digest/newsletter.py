"""Step: the daily email. Same five stories as the site's briefing, sent once a day through Kit.

Without a KIT_API_KEY it only writes pipeline/data/newsletter-preview.html so the layout can
be checked. With a key it sends when the run falls inside the configured UTC hour and no
broadcast has gone out for today's date.
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime

import requests
from sqlalchemy import insert, select

from . import config, db
from .textutil import first_sentences

log = logging.getLogger("digest.newsletter")

KIT_API = "https://api.kit.com/v4/broadcasts"


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def _plain(md: str | None, sentences: int = 2) -> str:
    text = (md or "").replace("#", "").replace("*", "").replace("_", "")
    return " ".join(first_sentences(text, sentences))


def render_html(briefing: dict, stories: dict[int, dict], date: datetime) -> tuple[str, str, str]:
    site = config.SITE_URL
    top = [stories[i] for i in briefing["storyIds"] if i in stories]
    also = [stories[i] for i in briefing["alsoIds"] if i in stories]
    stats = briefing["stats"]
    day = date.strftime("%A %d %B %Y")
    subject = top[0]["headline"] if top else f"AI news for {day}"
    preview = "; ".join(s["headline"] for s in top[1:3]) if len(top) > 1 else "The AI stories that matter today."

    def story_block(s: dict) -> str:
        url = f"{site}/story/{s['slug']}"
        srcs = ", ".join(dict.fromkeys(a.get("source") or a["domain"] for a in s["articles"]))
        badge = " · primary source inside" if s.get("hasPrimary") else ""
        points = ""
        if s.get("discussions"):
            p = s["discussions"][0].get("points")
            points = f" · Hacker News {p} points" if p else ""
        why = f'<p style="margin:8px 0 0;color:#4a5764;font-size:15px;line-height:1.5"><b style="color:#0b6e69">Why it matters</b> {_esc(s.get("whyItMatters"))}</p>' if s.get("whyItMatters") else ""
        return f"""
<tr><td style="padding:22px 0;border-top:1px solid #e3e7ec">
  <div style="font:500 11px/1 'JetBrains Mono',Consolas,monospace;letter-spacing:.1em;text-transform:uppercase;color:#7a8793">{_esc(s.get('categoryName'))}</div>
  <h2 style="margin:8px 0 8px;font:600 24px/1.2 Georgia,'Times New Roman',serif;color:#16202a"><a href="{url}" style="color:#16202a;text-decoration:none">{_esc(s['headline'])}</a></h2>
  <p style="margin:0;font-size:16px;line-height:1.55;color:#2b3641">{_esc(_plain(s.get('summaryMd'), 3))}</p>
  {why}
  <p style="margin:10px 0 0;font-size:13px;color:#7a8793">{_esc(srcs)}{badge}{points} · <a href="{url}" style="color:#0b6e69">Read the digest and sources →</a></p>
</td></tr>"""

    also_html = "".join(
        f'<li style="margin:0 0 8px"><a href="{site}/story/{s["slug"]}" style="color:#16202a;text-decoration:none;font-weight:600">{_esc(s["headline"])}</a> <span style="color:#7a8793;font-size:13px">· {_esc(s.get("categoryName"))}</span></li>'
        for s in also
    )
    body = f"""<!doctype html><html><body style="margin:0;background:#f4f6f8;font-family:'Source Sans 3','Segoe UI',Helvetica,Arial,sans-serif;color:#16202a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid #e3e7ec;border-radius:6px">
<tr><td style="padding:26px 28px 10px">
  <div style="font:600 30px/1 Georgia,'Times New Roman',serif;letter-spacing:-.02em">Digest <span style="color:#2442d6">AI</span> <span style="font:500 11px/1 'JetBrains Mono',Consolas,monospace;letter-spacing:.14em;color:#7a8793">DAILY BRIEFING</span></div>
  <div style="margin-top:10px;font:500 12px/1.6 'JetBrains Mono',Consolas,monospace;color:#7a8793">{_esc(day)} · {stats['stories']} stories · {stats['articles']} articles · {stats['minutes']} min read</div>
</td></tr>
<tr><td style="padding:0 28px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(story_block(s) for s in top)}</table></td></tr>
<tr><td style="padding:18px 28px 6px;border-top:2px solid #16202a">
  <div style="font:500 11px/1 'JetBrains Mono',Consolas,monospace;letter-spacing:.1em;text-transform:uppercase;color:#7a8793;margin-bottom:10px">Also today</div>
  <ul style="margin:0;padding-left:18px;font-size:15px;line-height:1.5">{also_html}</ul>
</td></tr>
<tr><td style="padding:22px 28px 26px;font-size:13px;color:#7a8793;line-height:1.6">
  Every story on <a href="{site}" style="color:#0b6e69">digestai.news</a> links to its sources and the discussion around it. Digests are generated from those sources by our editorial model; the sources are the record.<br>
  Read today's briefing on the web: <a href="{site}/today" style="color:#0b6e69">{site}/today</a>
</td></tr>
</table></td></tr></table></body></html>"""
    return subject, preview, body


def send_via_kit(subject: str, preview: str, body: str, when: datetime) -> dict:
    resp = requests.post(
        KIT_API,
        headers={"X-Kit-Api-Key": config.KIT_API_KEY, "Content-Type": "application/json"},
        json={
            "subject": subject,
            "preview_text": preview,
            "description": f"Daily briefing {when.date().isoformat()}",
            "content": body,
            "public": True,
            "published_at": when.isoformat(),
            "send_at": when.isoformat(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("broadcast", resp.json())


def run() -> dict:
    stats = {"sent": False, "preview": None, "reason": None}
    briefing_file = config.SITE_DATA_DIR / "briefing.json"
    stories_file = config.SITE_DATA_DIR / "stories.json"
    if not briefing_file.exists() or not stories_file.exists():
        stats["reason"] = "no export yet"
        return stats
    briefing = json.loads(briefing_file.read_text(encoding="utf-8"))
    stories = {s["id"]: s for s in json.loads(stories_file.read_text(encoding="utf-8"))}
    now = db.utcnow()
    if not briefing["storyIds"]:
        stats["reason"] = "no stories for a briefing"
        return stats

    subject, preview, body = render_html(briefing, stories, now)
    preview_path = config.DATA_DIR / "newsletter-preview.html"
    preview_path.write_text(body, encoding="utf-8")
    stats["preview"] = str(preview_path)

    if not config.KIT_API_KEY:
        stats["reason"] = "no KIT_API_KEY"
        return stats
    if now.hour != config.NEWSLETTER_HOUR_UTC:
        stats["reason"] = f"outside send hour ({config.NEWSLETTER_HOUR_UTC}:00 UTC)"
        return stats

    eng = db.engine()
    today = now.date().isoformat()
    with eng.connect() as conn:
        if conn.execute(select(db.newsletters.c.id).where(db.newsletters.c.date == today)).first():
            stats["reason"] = "already sent today"
            return stats
    try:
        result = send_via_kit(subject, preview, body, now)
    except requests.HTTPError as exc:
        stats["reason"] = f"kit error {exc.response.status_code}: {exc.response.text[:200]}"
        log.error(stats["reason"])
        return stats
    with eng.begin() as conn:
        conn.execute(insert(db.newsletters).values(
            date=today, subject=subject, story_ids=briefing["storyIds"],
            broadcast_id=str(result.get("id")), public_url=result.get("public_url"),
            sent_at=now, created_at=now,
        ))
    stats.update(sent=True, broadcast_id=result.get("id"), public_url=result.get("public_url"))
    return stats
