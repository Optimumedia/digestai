"""Entry point: python -m digest.run [all|fetch|extract|gate|enrich|cluster|rank|export]"""
from __future__ import annotations

import json
import logging
import sys
import time

from sqlalchemy import insert, update

from . import admin, audio, cluster, db, discuss, enrich, export, extract, fetch, gate, images, indexnow, newsletter, notify, pulse, gsc, intros, push, rank, social, threads, topics

STEPS = {
    "admin": admin.run,
    "notify": notify.run,
    "indexnow": indexnow.run,
    "topics": topics.run,
    "intros": intros.run,
    "push": push.run,
    "audio": audio.run,
    "social": social.run,
    "gsc": gsc.run,
    "fetch": fetch.run,
    "extract": extract.run,
    "gate": gate.run,
    "enrich": enrich.run,
    "cluster": cluster.run,
    "threads": threads.run,
    "discuss": discuss.run,
    "pulse": pulse.run,
    "rank": rank.run,
    "export": export.run,
    "images": images.run,
    "newsletter": newsletter.run,
}
ORDER = ["fetch", "extract", "gate", "enrich", "cluster", "threads", "discuss", "pulse", "rank", "export", "push", "topics", "intros", "images", "audio", "social", "newsletter", "gsc", "admin", "notify", "indexnow"]


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
            logging.getLogger("digest").exception("step %s crashed", step)
            stats = {"crashed": True}
        stats["seconds"] = round(time.time() - t0, 1)
        # Estimated kilobytes read from the database (the free plan meters them); the first step
        # also carries the connection's start-up reads.
        stats["readKB"] = round((db.bytes_read() - b0 + startup_bytes) / 1024, 1)
        startup_bytes = 0
        summary[step] = stats
        with eng.begin() as conn:
            conn.execute(update(db.runs).where(db.runs.c.id == run_id).values(finished_at=db.utcnow(), stats=stats))
        logging.getLogger("digest").info("%s: %s", step, json.dumps(stats, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 1 if any(s.get("crashed") for s in summary.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
