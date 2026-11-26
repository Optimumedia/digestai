"""Run every test file: python tests/run_all.py [--group all|python|site] [--strict] [--jobs N] [names...]

CI used to run a hand-written list of files, so a new test file could be added and never run (in
Nov 2026 nine of the 35 files had never run in CI). This finds every tests/test_*.py itself.

Each file runs as its own process, exactly as `python tests/test_x.py` does, and passes when:
- it exits 0;
- every top-level test_ function it defines reported PASS or ok (a file without the usual
  `if __name__ == "__main__"` loop, or a test hidden by a second function of the same name, would
  otherwise pass without running anything);
- with --strict, it printed no SKIP line except the ones --allow-skip names. A check that cannot run
  in CI (no Node, no built site) then fails instead of passing quietly.
The run also fails (with --strict; otherwise it warns) when any test wrote into pipeline/data or
site/src/data, which belong to a real run.

Groups: "site" is the files that read the built site (site/dist); CI runs them after building the
site from the test fixture (tests/site_fixture.py). "python" is every other file. "all" is both.

With TEST_POSTGRES_URL set, the files that mention it first run one at a time on that Postgres
(each drops and recreates its schema), then every file runs again on SQLite, in parallel."""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent

# Files that cannot run in CI, with the reason. Every other test_*.py runs. Keep this empty if you can.
NOT_IN_CI: dict[str, str] = {}

# A SKIP line the runner accepts even with --strict: (file name or "*", text the line contains).
EXPECTED_SKIPS: list[tuple[str, str]] = []

REPORT = re.compile(r"^(PASS|ok|FAIL)\s+(test_\w+)", re.M)
SKIP = re.compile(r"^.*\bSKIP\b.*$", re.M)
PER_FILE_TIMEOUT = 600


# Folders a test must never write to: the runner's cache and the site's data are a real run's.
GUARDED = [PIPELINE / "data", PIPELINE.parent / "site" / "src" / "data"]


def snapshot() -> dict[str, tuple[int, int]]:
    out = {}
    for root in GUARDED:
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    st = p.stat()
                    out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


def touched(before: dict, after: dict) -> list[str]:
    root = str(PIPELINE.parent)
    return sorted(p.replace(root, "").lstrip("\\/") for p in set(before) | set(after) if before.get(p) != after.get(p))


def discover() -> list[Path]:
    return sorted(HERE.glob("test_*.py"))


def reads_built_site(path: Path) -> bool:
    """A file that looks at site/dist belongs to the site group."""
    text = path.read_text(encoding="utf-8")
    return '"dist"' in text or bool(re.search(r"\bDIST\b", text))


def uses_postgres(path: Path) -> bool:
    return "TEST_POSTGRES_URL" in path.read_text(encoding="utf-8")


def defined_tests(path: Path) -> tuple[list[str], list[str], bool]:
    """(top-level test_ function names, names defined twice, whether there is a __main__ block)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    dupes = sorted({n for n in names if names.count(n) > 1})
    has_main = any(isinstance(n, ast.If) and "__main__" in ast.unparse(n.test) for n in tree.body)
    return names, dupes, has_main


@dataclass
class Result:
    path: Path
    label: str
    ok: bool
    seconds: float
    passed: int = 0
    problems: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    output: str = ""


def allowed_skip(name: str, line: str, allow: list[str]) -> bool:
    if any(a in line for a in allow):
        return True
    return any((f in ("*", name)) and text in line for f, text in EXPECTED_SKIPS)


def run_file(path: Path, label: str, env: dict, strict: bool, allow: list[str]) -> Result:
    t0 = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, str(path)], cwd=PIPELINE, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=PER_FILE_TIMEOUT)
        out, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as exc:
        out, code = f"{exc.stdout or ''}{exc.stderr or ''}\n(timed out after {PER_FILE_TIMEOUT}s)", -1
    res = Result(path, label, True, time.monotonic() - t0, output=out)
    names, dupes, has_main = defined_tests(path)
    reported = {}
    for status, name in REPORT.findall(out):
        reported[name] = status
    res.passed = sum(1 for s in reported.values() if s != "FAIL")
    if code != 0:
        res.problems.append(f"exited with code {code}")
    if not has_main:
        res.problems.append("has no `if __name__ == \"__main__\"` block, so running it runs no test")
    for d in dupes:
        res.problems.append(f"defines {d} twice: only the second one ever runs")
    if code == 0:
        missing = [n for n in names if n not in reported]
        if missing:
            res.problems.append(f"never reported {', '.join(missing)}")
    res.skips = [ln.strip() for ln in SKIP.findall(out)]
    if strict:
        for line in res.skips:
            if not allowed_skip(path.name, line, allow):
                res.problems.append(f"skipped a check: {line}")
    res.ok = not res.problems
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="*", help="only these files (test_x or test_x.py)")
    ap.add_argument("--group", choices=["all", "python", "site"], default="all")
    ap.add_argument("--strict", action="store_true", help="a SKIP line fails the file (CI)")
    ap.add_argument("--allow-skip", action="append", default=[], metavar="TEXT", help="accept SKIP lines containing TEXT")
    ap.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    ap.add_argument("--ci", action="store_true", help="leave out NOT_IN_CI files")
    ap.add_argument("--list", action="store_true", help="print the files that would run and stop")
    args = ap.parse_args(argv)

    files = discover()
    if args.names:
        wanted = {n if n.endswith(".py") else f"{n}.py" for n in args.names}
        unknown = wanted - {f.name for f in files}
        if unknown:
            print(f"no such test file: {', '.join(sorted(unknown))}")
            return 2
        files = [f for f in files if f.name in wanted]
    if args.group != "all":
        files = [f for f in files if reads_built_site(f) == (args.group == "site")]
    if args.ci:
        for f in files:
            if f.name in NOT_IN_CI:
                print(f"NOT RUN {f.name}: {NOT_IN_CI[f.name]}")
        files = [f for f in files if f.name not in NOT_IN_CI]
    if args.list:
        for f in files:
            print(f.name, "(site)" if reads_built_site(f) else "", "(postgres)" if uses_postgres(f) else "")
        return 0
    if not files:
        print("no test files selected")
        return 2

    base = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    pg_url = base.pop("TEST_POSTGRES_URL", None)
    t0 = time.monotonic()
    results: list[Result] = []
    before = snapshot()

    def report(r: Result) -> None:
        mark = "ok  " if r.ok else "FAIL"
        extra = f", {len(r.skips)} skipped" if r.skips else ""
        print(f"{mark} {r.path.name:<34} {r.label:<8} {r.passed:>3} tests{extra}  {r.seconds:5.1f}s", flush=True)

    if pg_url:
        for f in [f for f in files if uses_postgres(f)]:
            r = run_file(f, "postgres", dict(base, TEST_POSTGRES_URL=pg_url), args.strict, args.allow_skip)
            report(r)
            results.append(r)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for r in pool.map(lambda f: run_file(f, "sqlite" if pg_url else "", base, args.strict, args.allow_skip), files):
            report(r)
            results.append(r)

    failed = [r for r in results if not r.ok]
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    leaked = touched(before, snapshot())
    if leaked:
        print(f"\n{'FAIL' if args.strict else 'WARNING'}: the tests wrote into a real run's folders: {', '.join(leaked[:10])}")
        if gha:
            print(f"::error::tests wrote into {', '.join(leaked[:5])}")
    for r in failed:
        print(f"\n===== {r.path.name} ({r.label or 'run'}) =====")
        for p in r.problems:
            print(f"  - {p}")
        print(r.output.rstrip()[-6000:])
        if gha:
            print(f"::error file=pipeline/tests/{r.path.name}::{r.path.name}: {'; '.join(r.problems)[:900]}")
    total = sum(r.passed for r in results)
    skipped = sum(len(r.skips) for r in results)
    print(f"\n{len(results)} runs of {len(files)} files, {total} tests passed, {skipped} SKIP lines, "
          f"{len(failed)} failed, {time.monotonic() - t0:.0f}s")
    return 1 if failed or (leaked and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
