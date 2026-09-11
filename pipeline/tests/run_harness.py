"""Run the extraction harness against live URLs and report precision problems.

usage: python tests/run_harness.py [--url URL]   (from the pipeline/ directory)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest.extract import extract, fetch_html, domain_rule  # noqa: E402
from digest.textutil import clean_title  # noqa: E402


def run_one(url: str, title: str = "") -> tuple[bool, str, object]:
    html, err = fetch_html(url, agent=domain_rule(url).get("agent"))
    if err:
        return False, err, None
    if not title:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        og = soup.find("meta", attrs={"property": "og:title"})
        title = clean_title(og["content"] if og and og.get("content") else (soup.title.string if soup.title else ""))
    res = extract(url, html, title)
    return res.ok, res.reason or "", res


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--url":
        ok, reason, res = run_one(sys.argv[2])
        print(f"ok={ok} method={getattr(res, 'method', None)} words={getattr(res, 'words', 0)} reason={reason}")
        if res and res.notes:
            print("notes:", "; ".join(res.notes))
        if res and res.markdown:
            print("-" * 70)
            print(res.markdown[:1500])
            print("...")
            print(res.markdown[-800:])
        return 0

    cases = yaml.safe_load((Path(__file__).with_name("harness.yaml")).read_text(encoding="utf-8")).get("cases") or []
    if not cases:
        print("harness.yaml has no cases yet")
        return 0
    failures = 0
    for case in cases:
        ok, reason, res = run_one(case["url"], case.get("title", ""))
        problems = []
        if not ok:
            problems.append(f"extraction failed: {reason}")
        else:
            text = res.text.lower()
            for frag in case.get("must_contain", []):
                if frag.lower() not in text:
                    problems.append(f"missing: {frag[:50]!r}")
            for frag in case.get("must_not_contain", []):
                if frag.lower() in text:
                    problems.append(f"leaked: {frag[:50]!r}")
            lo, hi = case.get("min_words", 100), case.get("max_words", 8000)
            if not (lo <= res.words <= hi):
                problems.append(f"words {res.words} outside {lo}-{hi}")
        status = "PASS" if not problems else "FAIL"
        failures += bool(problems)
        print(f"{status} {case['url'][:90]}")
        for p in problems:
            print(f"     - {p}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
