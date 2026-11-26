"""Shared pieces for the test files (not a test file itself: run_all.py runs only test_*.py).

    from support import DIST, SITE, main, node, skip

- SITE, DIST, SITE_DATA: the site folder, its build output and the pipeline export it builds from.
- node(): the Node executable, or None.
- skip(reason): prints "SKIP <reason>". The runner (run_all.py --strict, as CI runs it) fails a file
  that prints a SKIP line it was not told to expect, so a check that cannot run never passes quietly.
- main(globals()): the loop every test file ends with: runs each test_ function, prints PASS or FAIL
  with the reason, exits 1 when anything failed.
"""
from __future__ import annotations

import shutil
import sys
import traceback
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
ROOT = PIPELINE.parent
SITE = ROOT / "site"
DIST = SITE / "dist"
SITE_DATA = SITE / "src" / "data"

if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))


def node() -> str | None:
    return shutil.which("node")


def skip(reason: str) -> None:
    print(f"SKIP {reason}")


def main(namespace: dict) -> None:
    failures = 0
    for name, fn in list(namespace.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - report every test, not just the first error
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:500])
                traceback.print_exc(limit=4)
    sys.exit(1 if failures else 0)
