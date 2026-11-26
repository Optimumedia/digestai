"""Related reading on story pages: python tests/test_site_related.py

site/src/lib/related.mjs (run in Node) indexes the stories once instead of scoring every story on every
story page. It must give exactly the answers of the full scan it replaced, in the same order, for
stories with shared names, repeated names, no names, no category, no date or a bad date, and it must
be much faster on a site-sized set (the scan was 205 of the build's 263 seconds on 26 Nov 2026)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"

PROBE = r"""
let seed = 11;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const cats = ["models", "agents", "policy", "hardware", "work", null, undefined];
const make = (n) => Array.from({ length: n }, (_, i) => {
  const ents = { companies: [], models: [], people: [] };
  const k = Math.floor(rnd() * 7);
  for (let j = 0; j < k; j++) {
    const e = Math.floor(Math.pow(rnd(), 2) * (n / 2 + 5));
    ents[["companies", "models", "people"][e % 3]].push((rnd() < 0.2 ? "ENT " : "Ent ") + e);
  }
  if (k && rnd() < 0.1) ents.people.push(ents.companies[0] || ents.models[0] || ents.people[0]);
  const date = rnd() < 0.03 ? null : rnd() < 0.02 ? "not a date" : new Date(Date.UTC(2026, 10, 26) - rnd() * 90 * 864e5).toISOString();
  return { id: i + 1, category: cats[Math.floor(rnd() * cats.length)], entities: rnd() < 0.05 ? undefined : ents, updatedAt: date };
});
const out = { differ: 0, checked: 0, nonEmpty: 0 };
const small = make(600);
const find = rel.relatedFinder(small);
for (const s of small) for (const limit of [3, 5, 12]) {
  const a = rel.relatedByScan(small, s, limit).map((x) => x.id).join(",");
  const b = find(s, limit).map((x) => x.id).join(",");
  out.checked += 1;
  if (a) out.nonEmpty += 1;
  if (a !== b) out.differ += 1;
}
const big = make(7000);
const findBig = rel.relatedFinder(big);
let t = performance.now();
for (let i = 0; i < 60; i++) rel.relatedByScan(big, big[i], 12);
out.scanMs = (performance.now() - t) / 60;
findBig(big[0], 12);
t = performance.now();
for (let i = 0; i < 60; i++) findBig(big[i], 12);
out.indexMs = (performance.now() - t) / 60;
console.log(JSON.stringify(out));
"""


def _run() -> dict | None:
    if not shutil.which("node"):
        print("SKIP node not installed")
        return None
    lib = (SITE / "src" / "lib" / "related.mjs").as_uri()
    probe = f"import * as rel from {json.dumps(lib)};\n{PROBE}"
    res = subprocess.run([shutil.which("node"), "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[:800]
    return json.loads(res.stdout)


RESULT: dict | None = None


def _result() -> dict | None:
    global RESULT
    if RESULT is None:
        RESULT = _run()
    return RESULT


def test_index_gives_the_answers_of_the_full_scan():
    r = _result()
    if r is None:
        return
    assert r["checked"] == 1800 and r["nonEmpty"] > 1000, r
    assert r["differ"] == 0, r


def test_index_is_much_faster_on_a_site_sized_set():
    r = _result()
    if r is None:
        return
    assert r["indexMs"] * 5 < r["scanMs"], r


def test_story_pages_use_the_index():
    data = (SITE / "src" / "lib" / "data.ts").read_text(encoding="utf-8")
    assert "relatedFinder(stories)" in data
    assert "stories\n    .filter((s) => s.id !== story.id)" not in data  # the old scan is gone from data.ts
    # The rail's "Biggest this week" is sorted once per build, not on every page that shows it.
    rail = (SITE / "src" / "components" / "Rail.astro").read_text(encoding="utf-8")
    assert "biggestThisWeek(exclude" in rail and ".sort(" not in rail.split("---")[1]


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - report every test, not just the first error
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
