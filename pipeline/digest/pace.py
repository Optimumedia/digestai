"""How long a chained pipeline run waits before it starts: the "pace" job in .github/workflows/pipeline.yml.

Each run queues the next one when it ends. Until 26 Nov 2026 the next run then waited for the next
7, 27 or 47 past the hour. A run takes about 21 minutes, so it always ended just after the slot it
would have used and the next one waited almost 20 minutes for the slot after: runs started every 40
minutes, not every 20, and each run took half the share of the day's free-model budgets it could
have (enrich.allowance shares them out over RUNS_PER_DAY, 72, runs).

Now the previous run passes the time its pipeline job started, and the next one starts PACE_MINUTES
after that, or at once if that time has passed. Runs still never start closer together than
PACE_MINUTES, so there are at most 72 a day, as the budgets assume. A run queued without that time
(by an older copy of the workflow, or by hand) waits for the next slot as before.

Standard library only: the pace job runs it with the runner's own Python, before any set-up.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

PACE_MINUTES = 20
SLOTS = (7, 27, 47)


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def until_slot(now: float, slots=SLOTS) -> int:
    """Seconds to the next slot minute after this one (the old rule, kept as the fallback)."""
    t = datetime.fromtimestamp(now, timezone.utc)
    for slot in sorted(slots) + [min(slots) + 60]:
        if slot > t.minute:
            return (slot - t.minute) * 60 - t.second
    return 0


def seconds_to_wait(now: float, after, minutes: int = PACE_MINUTES, slots=SLOTS) -> tuple[int, str]:
    """(seconds to wait, why). `after`: Unix time the previous run's pipeline job started, or empty."""
    gap = max(1, minutes) * 60
    started = _int(after)
    # A start time from the future or from hours ago is not the previous run's: use the slots.
    if started and started <= now + 60 and now - started < 6 * 3600:
        ago = now - started
        wait = int(max(0, min(gap, started + gap - now)))
        return wait, f"the previous run started {ago / 60:.1f} minutes ago; runs start at least {minutes} minutes apart"
    wait = until_slot(now, slots)
    return wait, "no start time from the previous run: waiting for the next " + ", ".join(map(str, slots)) + " past the hour"


def main() -> int:
    minutes = _int(os.environ.get("PACE_MINUTES")) or PACE_MINUTES
    slots = tuple(m for m in (_int(x) for x in (os.environ.get("RUN_MINUTES") or "").split(",")) if m is not None and 0 <= m < 60) or SLOTS
    wait, why = seconds_to_wait(time.time(), os.environ.get("AFTER"), minutes, slots)
    print(f"waiting {wait}s: {why}", file=sys.stderr)
    print(wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
