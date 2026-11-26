"""The test runner itself: python tests/test_run_all.py

tests/run_all.py finds every test file and fails one that passes without testing: no main loop, a
test hidden by a second function of the same name, a test that never reports, or (with --strict) a
SKIP line nobody expected. These checks run it on small made-up test files."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_all  # noqa: E402
from support import main  # noqa: E402

LOOP = '''
if __name__ == "__main__":
    import sys
    bad = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                bad += 1
                print("FAIL", name, exc)
    sys.exit(1 if bad else 0)
'''


def _run(body: str, strict: bool = False, allow: list[str] | None = None) -> run_all.Result:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "test_made_up.py"
        path.write_text(body, encoding="utf-8")
        return run_all.run_file(path, "", dict(os.environ), strict, allow or [])


def test_a_healthy_file_passes_and_counts_its_tests():
    r = _run("def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n" + LOOP)
    assert r.ok and r.passed == 2, (r.problems, r.output)


def test_a_failing_test_fails_the_file():
    r = _run("def test_a():\n    assert 1 == 2, 'nope'\n" + LOOP)
    assert not r.ok and any("exited with code 1" in p for p in r.problems), r.problems


def test_a_file_without_a_main_loop_fails():
    r = _run("def test_a():\n    assert 1\n")
    assert not r.ok and any("__main__" in p for p in r.problems), r.problems
    assert any("never reported test_a" in p for p in r.problems), r.problems


def test_a_shadowed_test_fails():
    r = _run("def test_a():\n    assert 1 == 2\n\ndef test_a():\n    assert 1\n" + LOOP)
    assert not r.ok and any("defines test_a twice" in p for p in r.problems), r.problems


def test_a_test_the_loop_never_reaches_fails():
    # A hand-written main that calls one test and forgets the other exits 0.
    body = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 1\n\nif __name__ == '__main__':\n    test_a()\n    print('PASS test_a')\n"
    r = _run(body)
    assert not r.ok and any("never reported test_b" in p for p in r.problems), r.problems


def test_skips_fail_only_in_strict_mode_unless_allowed():
    body = "def test_a():\n    print('SKIP site not built')\n" + LOOP
    assert _run(body).ok
    strict = _run(body, strict=True)
    assert not strict.ok and any("skipped a check: SKIP site not built" in p for p in strict.problems), strict.problems
    assert _run(body, strict=True, allow=["site not built"]).ok


def test_every_test_file_is_found_and_grouped():
    names = {p.name for p in run_all.discover()}
    assert {"test_run_all.py", "test_units.py", "test_site_smoke.py"} <= names, names
    site = {p.name for p in run_all.discover() if run_all.reads_built_site(p)}
    assert {"test_site_sitemap.py", "test_site_api.py", "test_site_smoke.py", "test_share_images.py"} <= site, site
    assert "test_units.py" not in site and "test_run_all.py" not in site
    pg = {p.name for p in run_all.discover() if run_all.uses_postgres(p)}
    assert {"test_reads.py", "test_gate.py", "test_work_learn.py"} <= pg, pg
    # Helpers and benchmarks are not test files.
    assert not names & {"support.py", "run_all.py", "site_fixture.py", "bench_reads.py", "run_harness.py"}


def test_the_workflow_runs_the_runner_not_a_list():
    wf = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "postgres-tests.yml").read_text(encoding="utf-8")
    assert "tests/run_all.py" in wf and "--strict" in wf
    assert "python tests/test_" not in wf, "a hand-written list of test files is back in the workflow"
    assert "tests/site_fixture.py" in wf and "npm run build" in wf


if __name__ == "__main__":
    main(globals())
