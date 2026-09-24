"""Search-shaped pages on the site: python tests/test_site_search_pages.py

The indexing rules (site/src/lib/indexing.mjs, run in Node) for the AI at Work job pages
(/work/<job>) and the model pages (/models/<slug>), and that a topic hub under a tracked model's slug
is never a second indexable page for the same model."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"


def _run(script: str) -> dict:
    lib = (SITE / "src" / "lib" / "indexing.mjs").as_uri()
    probe = f"import * as lib from {json.dumps(lib)};\n{script}"
    res = subprocess.run([shutil.which("node"), "--input-type=module", "-e", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    return json.loads(res.stdout)


def _card(tool: str, jobs: list[str]) -> dict:
    return {"tool": tool, "maker": None, "jobs": jobs}


def test_job_and_model_pages_follow_the_thin_rules():
    if not shutil.which("node"):
        print("SKIP node not installed")
        return
    stories = [
        {"id": 1, "slug": "launch", "articles": [{}], "entities": {}, "workCard": _card("A", ["business", "support"])},
        {"id": 2, "slug": "follow-up", "articles": [{}], "workCard": _card("B", ["business"])},
        {"id": 3, "slug": "third", "articles": [{}], "workCard": _card("C", ["business"])},
        {"id": 4, "slug": "lone-launch", "articles": [{}]},
        {"id": 5, "slug": "specced", "articles": [{}]},
    ]
    entities = [
        {"name": "GPT-6 Astra", "kind": "models", "storyIds": [2, 3]},
        {"name": "Solo Model", "kind": "models", "storyIds": [4]},
    ]
    models = [
        {"name": "GPT-6 Astra", "lab": "OpenAI", "storySlug": "launch", "date": "2026-09-10", "license": None, "context": None},
        {"name": "Solo Model", "lab": "X", "storySlug": "lone-launch", "date": "2026-09-11", "license": None, "context": None},
        {"name": "Spec Model", "lab": "X", "storySlug": "specced", "date": "2026-09-12", "license": "MIT", "context": None},
    ]
    out = _run(
        f"const stories = {json.dumps(stories)}, entities = {json.dumps(entities)}, models = {json.dumps(models)};\n"
        "const no = lib.noindexPaths(stories, entities, models);\n"
        "const pages = Object.fromEntries([...lib.modelPages(stories, entities, models).values()].map((p) => [p.slug, [...p.storyIds].sort()]));\n"
        "console.log(JSON.stringify({no: [...no].filter((p) => /^\\/(work|models|topic)\\//.test(p)).sort(), pages, "
        "slugs: lib.JOB_SLUGS, counts: lib.jobCounts(stories)}));")
    # Job pages: one address per job; thin (noindex) below three cards or tools.
    assert out["slugs"] == {"customers": "get-customers", "content": "make-content", "sell": "sell",
                            "support": "support", "business": "run-the-business"}
    assert out["counts"]["business"] == {"cards": 3, "tools": 3}
    assert "/work/run-the-business" not in out["no"]
    for slug in ("get-customers", "make-content", "sell", "support"):
        assert f"/work/{slug}" in out["no"], out["no"]
    # Model pages: the tracker's launch story plus every story whose entities name the model.
    assert out["pages"]["gpt-6-astra"] == [1, 2, 3]
    assert "/models/gpt-6-astra" not in out["no"]
    assert "/models/solo-model" in out["no"]          # only its launch story, no spec: thin
    assert "/models/spec-model" not in out["no"]      # one story, but a licence: not thin
    # A topic hub under a tracked model's slug is a redirect page, never in the sitemap.
    assert "/topic/gpt-6-astra" in out["no"] and "/topic/solo-model" in out["no"]


def test_templates_use_the_shared_rules():
    job = (SITE / "src" / "pages" / "work" / "[job].astro").read_text(encoding="utf-8")
    model = (SITE / "src" / "pages" / "models" / "[slug].astro").read_text(encoding="utf-8")
    topic = (SITE / "src" / "pages" / "topic" / "[slug].astro").read_text(encoding="utf-8")
    assert "thin={jobThin(job.key)}" in job and '"ItemList"' in job
    assert "thin={!modelPageIndexable(page)}" in model and '"NewsArticle"' in model and "<dl>" in model
    assert "modelPageFor(slug!)" in topic and "<StoryRedirect" in topic
    # The sitemap plan (indexing.mjs sitemapIndex, used by astro.config.mjs) starts from the same noindex list.
    config = (SITE / "astro.config.mjs").read_text(encoding="utf-8")
    rules = (SITE / "src" / "lib" / "indexing.mjs").read_text(encoding="utf-8")
    assert "sitemapIndex({" in config and "plan.include(p)" in config
    assert "noindexPaths(stories, entities, models, priceChanges)" in rules
    # /work/prices is built and linked whatever the history holds, and indexable only once enough
    # tools have actually moved (WORK_PRICES_MIN_ENTRIES).
    assert "WORK_PRICES_MIN_ENTRIES" in rules and '"/work/prices"' in rules
    assert 'priceChanges: read("work-prices.json", {}).changes' in config


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
