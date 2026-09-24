"""Entry point: python -m digest.run [all|fetch|extract|gate|enrich|cluster|rank|export]"""
from __future__ import annotations

import importlib
import json
import logging
import sys
import time

from sqlalchemy import insert, update

from . import cache, db

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
ORDER = ["fetch", "extract", "gate", "enrich", "cluster", "threads", "discuss", "pulse", "rank", "upgrade", "repair", "export", "push", "topics", "intros", "images", "howto", "prices", "simplify", "audio", "media", "social", "newsletter", "gsc", "tidy", "backup", "admin", "morning", "notify", "indexnow"]


def step_fn(name: str):
    return importlib.import_module(f".{name}", __package__).run


STEPS = {name: (lambda name=name: step_fn(name)()) for name in ORDER}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    steps = ORDER if not argv or argv[0] == "all" else [s for s in argv if s in STEPS]
    if not steps:
        print(f"usage: python -m digest.run [{'|'.join(['all'] + ORDER)}]")
        return 2
    b_start = db.bytes_read()
    eng = db.engine()
    summary = {}
    startup_bytes = db.bytes_read() - b_start
    for step in steps:
        started = db.utcnow()
        with eng.begin() as conn:
            run_id = conn.execute(insert(db.runs).values(started_at=started, step=step)).inserted_primary_key[0]
        t0 = time.time()
        b0 = db.bytes_read()
        try:
            stats = STEPS[step]()
        except Exception:
            log.exception("step %s crashed", step)
            stats = {"crashed": True}
        stats["seconds"] = round(time.time() - t0, 1)
        # Estimated kilobytes read from the database (the free plan meters them); the first step
        # also carries the connection's start-up reads.
        stats["readKB"] = round((db.bytes_read() - b0 + startup_bytes) / 1024, 1)
        startup_bytes = 0
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
