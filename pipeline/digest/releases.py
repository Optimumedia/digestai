"""The model tracker's memory: every model release the export has ever listed.

The export builds the Models tracker from the stories in its window (EXPORT_DAYS, by last update), so
a model dropped off /models, its page /models/<slug> and every answer about it some weeks after its
launch: on 23 Nov 2026 the tracker began on 6 Oct, and September's launches had no page left. For
the questions people ask assistants ("when was X released", "which models came out in September")
the page has to stay.

Each run merges this run's rows into the kept ones and the export lists them all. A kept row keeps its
first date and launch story; a later row only fills specs the launch row left empty. Rows whose story
was unpublished (moderation.yaml) are dropped, and the tracker's current rules (trackers.py) are
applied to old rows again, so a rule fixed today also cleans what was kept.

Costs nothing to read: a small JSON file in the runner's cache (pipeline/data/cache), kept between runs
by the Actions cache. The archive step stores a copy inside archive.json.gz, which is published once a
day as a release asset (archive.py, media.py), so a lost cache loses at most a day.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger("digest.releases")

FORMAT = 1
ARCHIVE_KEY = "releases"
# Bounds the file whatever happens: about thirty years of launches at today's rate.
MAX_ROWS = 20000
SPEC_FIELDS = ("license", "context", "link", "lab")


def path() -> Path:
    return config.CACHE_DIR / "releases.json"


def read_file() -> dict[str, dict] | None:
    """The runner's copy, or None when it is missing or unreadable."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("format") == FORMAT and isinstance(data.get("rows"), dict):
        return data["rows"]
    return None


def load() -> dict[str, dict]:
    """Kept rows: the runner's copy, else the copy inside the archive (its daily backup), else none."""
    rows = read_file()
    if rows is not None:
        return rows
    try:
        from . import archive
        kept = archive._load().get(ARCHIVE_KEY)
        if isinstance(kept, dict):
            log.info("model releases taken from the archive's copy: %d", len(kept))
            return dict(kept)
    except Exception as exc:  # the backup is a nicety; never stop the export for it
        log.warning("archive copy of model releases unreadable: %s", str(exc)[:120])
    return {}


def save(rows: dict[str, dict]) -> None:
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_name(path().name + ".tmp")
    tmp.write_text(json.dumps({"format": FORMAT, "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path())


def merge(kept: dict[str, dict], current: dict[str, dict], join: Callable[[dict, dict], None] | None = None) -> dict[str, dict]:
    """This run's rows over the kept ones. The earlier report is the launch: its date, story and
    headline stay; specs it lacks come from the newer row; the source count is the larger one.
    `join` (trackers.join), when given, decides instead: it also keeps a launch story that names the
    model over an earlier one that does not."""
    out = dict(kept)
    for key, row in current.items():
        old = out.get(key)
        if join and old is not None:
            new = dict(old)
            join(new, row)
            out[key] = new
            continue
        if old is None or (row.get("date") or "~") <= (old.get("date") or "~"):
            new = dict(row)
            base = old or {}
        else:
            new = dict(old)
            base = row
        for f in SPEC_FIELDS:
            if not new.get(f) and base.get(f):
                new[f] = base[f]
        new["sources"] = max(int(row.get("sources") or 0), int((old or {}).get("sources") or 0))
        out[key] = new
    return out


def remember(current: dict[str, dict], unpublished: set[str] | list[str],
             is_release: Callable[[dict, str], bool], fold: Callable[[dict], dict],
             key: Callable[[str, str | None], str] | None = None,
             join: Callable[[dict, dict], None] | None = None) -> dict[str, dict]:
    """Every release kept so far plus this run's, by tracker key. `is_release`, `fold` and `key` are the
    tracker's own rules (trackers.py), passed in so the rules live in one place."""
    gone = set(unpublished or [])
    # The export tested each row against its story's headline and the article's title; a kept row has
    # the headline only, so the name stands in for the title. The rules on the row's own fields (a real
    # maker, a usable model, not a comparison) still apply in full.
    kept: dict[str, dict] = {}
    for k, r in load().items():
        if not (isinstance(r, dict) and r.get("storySlug") not in gone
                and is_release(r, f"{r.get('storyHeadline') or ''} | {r.get('name') or ''}")):
            continue
        # Keyed again by today's rules, so a key rule fixed today ("OpenAI Group PBC" is OpenAI) joins
        # the rows it now calls one model instead of listing both.
        kept = merge(kept, {key(r.get("name") or "", r.get("lab")) if key else k: r}, join)
    rows = fold(merge(kept, current, join))
    if len(rows) > MAX_ROWS:
        rows = dict(sorted(rows.items(), key=lambda kv: kv[1].get("date") or "", reverse=True)[:MAX_ROWS])
    try:
        save(rows)
    except OSError as exc:
        log.warning("model releases not saved: %s", str(exc)[:120])
    return rows
