"""Step: pull Google Search Console data into the dashboard (clicks, impressions, position,
top queries and pages, indexing coverage of the sitemaps). Needs a Google service account
JSON in GSC_SERVICE_ACCOUNT_JSON that has been added as a user of the Search Console
property. Skips quietly when it is not configured."""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

import requests

from . import config, db

log = logging.getLogger("digest.gsc")

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
API = "https://searchconsole.googleapis.com/webmasters/v3"


def _token(sa_info: dict) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


def run() -> dict:
    stats = {"configured": False}
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "").strip()
    key_file = config.ROOT / "gsc-service-account.json"  # local runs: the downloaded key file
    if not raw and key_file.exists():
        raw = key_file.read_text(encoding="utf-8").strip()
    if not raw:
        return stats
    try:
        sa = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("GSC_SERVICE_ACCOUNT_JSON is not valid JSON")
        return stats
    stats["configured"] = True
    prop = os.environ.get("GSC_PROPERTY") or f"sc-domain:{config.SITE_URL.split('//', 1)[-1]}"
    try:
        token = _token(sa)
    except Exception as exc:  # noqa: BLE001
        log.warning("GSC auth failed: %s", str(exc)[:160])
        stats["error"] = "auth"
        return stats
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    if not os.environ.get("GSC_PROPERTY"):
        # Use whichever property the service account was actually added to: the domain
        # property if it exists, otherwise the URL-prefix property (meta-tag verification).
        try:
            host = config.SITE_URL.split("//", 1)[-1]
            sites = [x.get("siteUrl", "") for x in s.get(f"{API}/sites", timeout=30).json().get("siteEntry", [])]
            mine = [x for x in sites if host in x]
            prop = next((x for x in mine if x.startswith("sc-domain:")), mine[0] if mine else prop)
        except Exception as exc:  # noqa: BLE001
            log.warning("GSC site list failed: %s", str(exc)[:120])
    end = (db.utcnow() - timedelta(days=2)).date()  # GSC data lags ~2 days
    start = end - timedelta(days=27)
    out: dict = {"property": prop, "start": start.isoformat(), "end": end.isoformat()}

    def query(body: dict):
        r = s.post(f"{API}/sites/{requests.utils.quote(prop, safe='')}/searchAnalytics/query", json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:160]}")
        return r.json().get("rows", [])

    try:
        by_day = query({"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date"], "rowLimit": 100})
        out["perDay"] = [{"day": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"], "ctr": round(r["ctr"], 4), "position": round(r["position"], 1)} for r in by_day]
        q = query({"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query"], "rowLimit": 50})
        out["queries"] = [{"query": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"], "ctr": round(r["ctr"], 4), "position": round(r["position"], 1)} for r in q]
        p = query({"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["page"], "rowLimit": 50})
        out["pages"] = [{"page": r["keys"][0].replace(config.SITE_URL, ""), "clicks": r["clicks"], "impressions": r["impressions"], "ctr": round(r["ctr"], 4), "position": round(r["position"], 1)} for r in p]
        sm = s.get(f"{API}/sites/{requests.utils.quote(prop, safe='')}/sitemaps", timeout=30)
        out["sitemaps"] = [{"path": x.get("path", "").replace(config.SITE_URL, ""), "lastSubmitted": x.get("lastSubmitted"), "errors": x.get("errors"), "warnings": x.get("warnings"),
                            "submitted": sum(int(c.get("submitted", 0)) for c in x.get("contents", [])), "indexed": sum(int(c.get("indexed", 0)) for c in x.get("contents", []))}
                           for x in sm.json().get("sitemap", [])] if sm.ok else []
        out["totals"] = {"clicks": sum(r["clicks"] for r in out["perDay"]), "impressions": sum(r["impressions"] for r in out["perDay"])}
    except Exception as exc:  # noqa: BLE001
        log.warning("GSC query failed: %s", str(exc)[:200])
        stats["error"] = str(exc)[:120]
        return stats
    (config.SITE_DATA_DIR / "gsc.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    stats.update(clicks=out["totals"]["clicks"], impressions=out["totals"]["impressions"], queries=len(out["queries"]))
    return stats
