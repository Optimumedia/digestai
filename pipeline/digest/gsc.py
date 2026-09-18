"""Step: pull Google Search Console data into the dashboard (clicks, impressions, position,
top queries and pages, indexing coverage of the sitemaps). Needs a Google service account
JSON in GSC_SERVICE_ACCOUNT_JSON that has been added as a user of the Search Console
property. Skips quietly when it is not configured."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

import requests

from . import config, db
from .history import day_range

log = logging.getLogger("digest.gsc")

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
API = "https://searchconsole.googleapis.com/webmasters/v3"


def _token(sa_info: dict) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


LIST_ROWS = 1000  # queries or pages asked for per 28-day window; the file keeps the top TOP_ROWS
TOP_ROWS = 50
DAY_QUERY_ROWS = 10000  # date x query rows: plenty for a young site, bounded for a big one
SPARK_QUERIES = 15  # queries that carry their daily positions in gsc.json


def weighted_position(rows) -> float | None:
    """Average position over several rows, weighted by impressions: a day with 100 impressions
    counts 100 times as much as a day with one. None when Google showed the site nowhere."""
    rows = [r for r in rows if r.get("position") is not None and r.get("impressions")]
    imp = sum(r["impressions"] for r in rows)
    if not imp:
        return None
    return round(sum(r["position"] * r["impressions"] for r in rows) / imp, 1)


def _row(r: dict, key: str, value: str) -> dict:
    return {key: value, "clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
            "ctr": round(r["ctr"], 4), "position": round(r["position"], 1)}


def build(by_day: list, by_day_query: list, queries: list, prev_queries: list, pages: list, prev_pages: list,
          start: str, end: str) -> dict:
    """Turn Search Console rows into the ranking part of gsc.json. Search Console leaves out days
    without impressions; they are filled in with no position (never position 0), so charts can
    leave them as gaps. `prev_*` are the 28 days before `start`, for the change in position."""
    n_queries: dict[str, int] = {}
    daily: dict[str, list] = {}
    for r in by_day_query:
        d, q = r["keys"][0], r["keys"][1]
        if r["impressions"] > 0:
            n_queries[d] = n_queries.get(d, 0) + 1
            daily.setdefault(q, []).append([d, int(r["impressions"]), round(r["position"], 1)])
    got = {r["keys"][0]: r for r in by_day}
    first = min(got) if got else start
    history, per_day = [], []
    for d in day_range(min(first, start), end):
        r = got.get(d)
        imp = int(r["impressions"]) if r else 0
        row = {"day": d, "clicks": int(r["clicks"]) if r else 0, "impressions": imp,
               "position": round(r["position"], 2) if r and imp else None}
        if d >= start:
            row["queries"] = n_queries.get(d, 0)
        if d >= first:
            history.append(row)
        if d >= start:
            per_day.append({**row, "ctr": round(r["ctr"], 4) if r and imp else 0,
                            "position": round(row["position"], 1) if row["position"] is not None else None})
    before_q = {r["keys"][0]: r for r in prev_queries}
    before_p = {r["keys"][0]: r for r in prev_pages}

    def with_prev(row: dict, old: dict | None) -> dict:
        if old and old["impressions"] > 0:
            row.update(prevImpressions=int(old["impressions"]), prevClicks=int(old["clicks"]), prevPosition=round(old["position"], 1))
        return row

    order = lambda r: (-r["impressions"], -r["clicks"], r["keys"][0])  # noqa: E731
    out_q = []
    for i, r in enumerate(sorted(queries, key=order)[:TOP_ROWS]):
        row = with_prev(_row(r, "query", r["keys"][0]), before_q.get(r["keys"][0]))
        if i < SPARK_QUERIES:
            row["days"] = sorted(daily.get(r["keys"][0], []))  # [day, impressions, position]
        out_q.append(row)
    out_p = [with_prev(_row(r, "page", r["keys"][0].replace(config.SITE_URL, "") or "/"), before_p.get(r["keys"][0]))
             for r in sorted(pages, key=order)[:TOP_ROWS]]
    seen = lambda rows: sum(1 for r in rows if r["impressions"] > 0)  # noqa: E731
    return {
        "history": history, "perDay": per_day, "queries": out_q, "pages": out_p,
        # Google leaves out rare searches for privacy, so the query count is a floor.
        "queryCount": seen(queries), "prevQueryCount": seen(prev_queries),
        "pageCount": seen(pages), "prevPageCount": seen(prev_pages),
        "totals": {"clicks": sum(r["clicks"] for r in per_day), "impressions": sum(r["impressions"] for r in per_day),
                   "position": weighted_position(per_day)},
    }


KEY_PAGES = ["/","/today", "/listen", "/models", "/funding"]
TOP_STORIES_TO_INSPECT = 3


def _inspections(s, prop: str) -> list[dict]:
    """Google's index status for the key pages and the top stories (URL Inspection API).

    Google no longer fills in the sitemap report's indexed count (it is always 0), so asking page
    by page is the only reliable signal. 8 pages a run is about 400 a day; the API allows 2,000."""
    pages = list(KEY_PAGES)
    try:
        stories = json.loads((config.SITE_DATA_DIR / "stories.json").read_text(encoding="utf-8"))
        top = sorted(stories, key=lambda x: -(x.get("score") or 0))[:TOP_STORIES_TO_INSPECT]
        pages += [f"/story/{x['slug']}" for x in top if x.get("slug")]
    except (OSError, ValueError, TypeError):
        pass
    def one(page: str) -> dict | None:
        try:
            r = s.post("https://searchconsole.googleapis.com/v1/urlInspection/index:inspect", timeout=45,
                       json={"inspectionUrl": config.SITE_URL + page, "siteUrl": prop})
            if r.status_code >= 400:
                log.info("URL inspection of %s failed: %s %s", page, r.status_code, r.text[:120])
                return None
            x = r.json().get("inspectionResult", {}).get("indexStatusResult", {})
            return {"page": page, "state": x.get("coverageState") or "Unknown", "lastCrawl": x.get("lastCrawlTime")}
        except Exception as exc:  # noqa: BLE001 - index status is a nice-to-have
            log.info("URL inspection of %s failed: %s", page, str(exc)[:120])
            return None

    # Each inspection takes several seconds; asked one after another they added a minute to every run.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(pages)) as pool:
        return [c for c in pool.map(one, pages) if c]


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
    # The API keeps about 16 months of daily totals; the dashboard stores every day it gets
    # (daily_stats), so year-over-year comparisons keep working after they age out here.
    history_start = end - timedelta(days=480)
    out: dict = {"property": prop, "start": start.isoformat(), "end": end.isoformat()}

    def query(body: dict):
        r = s.post(f"{API}/sites/{requests.utils.quote(prop, safe='')}/searchAnalytics/query", json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:160]}")
        return r.json().get("rows", [])

    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=27)
    out.update(prevStart=prev_start.isoformat(), prevEnd=prev_end.isoformat())

    def window(dimension: str, a: date, b: date) -> list:
        return query({"startDate": a.isoformat(), "endDate": b.isoformat(), "dimensions": [dimension], "rowLimit": LIST_ROWS})

    try:
        by_day = query({"startDate": history_start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date"], "rowLimit": 1000})
        by_day_query = query({"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date", "query"], "rowLimit": DAY_QUERY_ROWS})
        out.update(build(by_day, by_day_query, window("query", start, end), window("query", prev_start, prev_end),
                         window("page", start, end), window("page", prev_start, prev_end), start.isoformat(), end.isoformat()))
        sm = s.get(f"{API}/sites/{requests.utils.quote(prop, safe='')}/sitemaps", timeout=30)
        out["sitemaps"] = [{"path": x.get("path", "").replace(config.SITE_URL, ""), "lastSubmitted": x.get("lastSubmitted"), "errors": x.get("errors"), "warnings": x.get("warnings"),
                            "submitted": sum(int(c.get("submitted", 0)) for c in x.get("contents", [])), "indexed": sum(int(c.get("indexed", 0)) for c in x.get("contents", []))}
                           for x in sm.json().get("sitemap", [])] if sm.ok else []
        out["inspections"] = _inspections(s, prop)
        out["inspectedAt"] = db.utcnow().isoformat()
    except Exception as exc:  # noqa: BLE001
        log.warning("GSC query failed: %s", str(exc)[:200])
        stats["error"] = str(exc)[:120]
        return stats
    (config.SITE_DATA_DIR / "gsc.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    stats.update(clicks=out["totals"]["clicks"], impressions=out["totals"]["impressions"], queries=out["queryCount"], position=out["totals"]["position"],
                 inspected=len(out["inspections"]),
                 indexed=sum(1 for c in out["inspections"] if c["state"].lower().startswith(("submitted and indexed", "indexed"))))
    return stats
