"""Entry point: python -m digest.run [all|fetch|extract|gate|enrich|cluster|rank|export]"""
from __future__ import annotations

import importlib
import json
import logging
import shutil
import sys
import time

from sqlalchemy import insert, update

from . import cache, config, db

log = logging.getLogger("digest")

# The steps, in the order a full run takes them. "upgrade" comes after "rank", which is what tells
# it which stories turned out to matter, and before "export", so a rewritten digest reaches the site
# in the same run. "morning" reads what "admin" and "export" just wrote, and "notify" opens its
# issue. "howto" (setup steps from a maker's own page for AI at Work cards) comes after "export", so
# its page fetches never delay the site update; what it finds reaches the site with the next export.
# "simplify" (plain-words rewrites of recent AI at Work cards, simplify.py) runs after it the same way.
# "prices" (what a tool costs, from the maker's own pricing page when the coverage did not say,
# prices.py) is the third of those: same bar, same budget, and the export already wrote the history
# the page reads, so a slow pricing page only costs this run's gap-filling.
# Each step is the run() of the module of the same name, imported when it starts:
# a run of one step (the workflow's export after an early stop) does not load the other 25 modules,
# and extract's HTML libraries (trafilatura, ~0.6 s) are loaded by the extract step alone.
ORDER = ["fetch", "extract", "gate", "enrich", "cluster", "threads", "discuss", "pulse", "rank", "upgrade", "repair", "export", "push", "topics", "intros", "images", "howto", "prices", "simplify", "audio", "media", "social", "newsletter", "gsc", "tidy", "backup", "admin", "team", "morning", "notify", "readme", "indexnow"]


def step_fn(name: str):
    return importlib.import_module(f".{name}", __package__).run


STEPS = {name: (lambda name=name: step_fn(name)()) for name in ORDER}

# Steps that can wait for the next run (20 minutes later) when time is short, with the seconds each
# needs to be worth starting. The others always run, and these seconds are kept for them, so a slow
# day costs topic descriptions or images, never the dashboard, the team or the archive's backup.
# On 23 Nov 2026 summaries took ten minutes, topics six, and the run was cut off before media,
# admin and the team ever ran.
CAN_WAIT = {"discuss": 20, "pulse": 10, "upgrade": 20, "repair": 10, "topics": 100, "intros": 30, "images": 60,
            "howto": 40, "prices": 30, "simplify": 40, "social": 20, "newsletter": 30, "gsc": 15, "tidy": 20}
# The daily spoken briefing always runs: it has its own limit (AUDIO_TIME_BUDGET_SECONDS) and takes a
# second once the day's episode exists, but as a step that could wait it was skipped behind ten
# minutes of summaries.
LAST = {"audio": 150, "media": 90, "backup": 25, "admin": 30, "team": 5, "morning": 20, "notify": 5, "readme": 5, "indexnow": 5}


# A step that reads at least this many KB names its two largest statements in its stats (readTop).
READ_TOP_KB = 50


def time_kept(steps: list[str], i: int) -> float:
    """Seconds kept for the steps after position i that always run."""
    return float(sum(LAST.get(s, 0) for s in steps[i + 1:]))


def should_wait(step: str, steps: list[str], i: int, elapsed: float, budget: float) -> bool:
    return step in CAN_WAIT and budget - elapsed - time_kept(steps, i) < CAN_WAIT[step]


def restore_admin() -> bool:
    """The admin step is near the end of a run. When the workflow's time limit stops a run before it
    (13 Oct: the first run after a week's stop was cut off at "media"), the site was built without
    admin.json and the dashboard said "No admin data yet". The last dashboard the admin step wrote
    is kept in the runner cache; it goes back in place here, showing its own age, until this run's
    admin step replaces it."""
    site, kept = config.SITE_DATA_DIR / "admin.json", config.CACHE_DIR / "admin.json"
    if site.exists() or not kept.exists():
        return False
    if cache.read_file(kept, dict, gz=False) is None:  # a damaged copy is moved aside, never copied to the site
        return False
    try:
        site.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kept, site)
        return True
    except OSError as exc:
        log.warning("could not put the last admin.json back: %s", exc)
        return False


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    steps = ORDER if not argv or argv[0] == "all" else [s for s in argv if s in STEPS]
    if not steps:
        print(f"usage: python -m digest.run [{'|'.join(['all'] + ORDER)}]")
        return 2
    restore_admin()
    b_start = db.bytes_read()
    eng = db.engine()
    summary = {}
    startup_bytes = db.bytes_read() - b_start
    run_start = time.time()
    timed = not argv or argv[0] == "all"
    for i, step in enumerate(steps):
        elapsed = time.time() - run_start
        if timed and should_wait(step, steps, i, elapsed, config.RUN_BUDGET_SECONDS):
            summary[step] = {"skipped": "time", "left": round(config.RUN_BUDGET_SECONDS - elapsed), "seconds": 0.0, "readKB": 0.0}
            log.info("%s: %s", step, json.dumps(summary[step]))
            continue
        started = db.utcnow()
        with eng.begin() as conn:
            run_id = conn.execute(insert(db.runs).values(started_at=started, step=step)).inserted_primary_key[0]
        t0 = time.time()
        b0 = db.bytes_read()
        damaged_before = len(cache.DAMAGED)
        by_statement = db.reads_by_statement()
        try:
            stats = STEPS[step]()
        except Exception:
            log.exception("step %s crashed", step)
            stats = {"crashed": True}
        if len(cache.DAMAGED) > damaged_before:
            # Runner files found damaged (and moved aside) during this step: named, never silent.
            stats["damagedFiles"] = sorted(set(cache.DAMAGED[damaged_before:]))
        stats["seconds"] = round(time.time() - t0, 1)
        # Estimated kilobytes read from the database (the free plan meters them); the first step
        # also carries the connection's start-up reads.
        stats["readKB"] = round((db.bytes_read() - b0 + startup_bytes) / 1024, 1)
        startup_bytes = 0
        if stats["readKB"] >= READ_TOP_KB:
            # Which statement read the most: where to look when the monthly reads climb.
            stats["readTop"] = db.top_reads(by_statement)
        summary[step] = stats
        with eng.begin() as conn:
            conn.execute(update(db.runs).where(db.runs.c.id == run_id).values(finished_at=db.utcnow(), stats=stats))
        log.info("%s: %s", step, json.dumps(stats, default=str))
        # Saved after every step: a run stopped by the time limit still leaves a usable copy.
        cache.save_all()
    print(json.dumps(summary, indent=2, default=str))
    return 1 if any(s.get("crashed") for s in summary.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
