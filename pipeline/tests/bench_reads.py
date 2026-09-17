"""How much a pipeline run reads from the database, on a synthetic SQLite copy at production scale.

    python tests/bench_reads.py --db C:/tmp/bench.db [--days 10 --stories-per-day 100] [--runs 3]

Builds the database once (published stories with ~8 KB texts and 384-dimension embeddings,
rejected articles that keep their text, threads, reader events, run history), then runs the
offline steps several times with a small batch of new articles before each run, and prints the
estimated kilobytes each step read (the readKB the pipeline records). Steps that need the network
(fetch, extract, discuss, pulse, social, newsletter, gsc) are left out; the model steps run without
keys (heuristic summaries, no names or descriptions).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
STEPS = ["gate", "enrich", "cluster", "threads", "rank", "upgrade", "export", "push", "topics", "intros", "tidy", "admin"]
WORDS = ("model agents inference open weights launch funding round chips data center regulation safety benchmark "
         "research paper robotics enterprise startup valuation release context window multimodal reasoning").split()
COMPANIES = ["OpenAI", "Anthropic", "Google", "Meta", "Nvidia", "Mistral", "xAI", "Microsoft", "Apple", "Amazon",
             "DeepSeek", "Cohere", "Perplexity", "Figure", "Scale", "Databricks", "Hugging Face", "Qwen"]


def ts(dt: datetime) -> str:
    """A timestamp as SQLAlchemy stores it in SQLite, so comparisons in SQL work as in the pipeline."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def text(rng: random.Random, chars: int) -> str:
    out, n = [], 0
    while n < chars:
        w = rng.choice(WORDS)
        out.append(w)
        n += len(w) + 1
    return " ".join(out)[:chars]


def unit(rng: random.Random, dim: int = 384, base: list[float] | None = None, noise: float = 1.0) -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(dim)]
    if base is not None:
        v = [b + noise * x / math.sqrt(dim) for b, x in zip(base, v)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def build(path: Path, days: int, per_day: int, rejected_per_day: int, seed: int = 7) -> None:
    import sqlite3

    from digest import db

    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    eng = db.engine()  # creates the schema
    con = sqlite3.connect(path)
    cur = con.cursor()
    sources = [(i, f"src{i}", f"Source {i}", f"https://src{i}.test/feed", "rss", rng.choice(["press", "primary", "newsletter", "community"]),
                None, 1.0, 1, 0, 1, 0, None, ts((now - timedelta(minutes=30))), None, 0, 1.0) for i in range(1, 67)]
    cur.executemany("insert into sources values (" + ",".join("?" * 17) + ")", sources)
    aid, sid, tid = 0, 0, 0
    arts, stories, threads = [], [], []
    for d in range(days, 0, -1):
        for _ in range(per_day):
            sid += 1
            first = now - timedelta(days=d, minutes=rng.randint(0, 1439))
            base = unit(rng)
            n = max(1, min(40, int(rng.expovariate(1 / 5))))
            ents = {"companies": rng.sample(COMPANIES, 2), "models": [f"Model-{rng.randint(1, 300)}"], "people": []}
            members = []
            for k in range(n):
                aid += 1
                emb = unit(rng, base=base, noise=0.3)
                pub = first + timedelta(hours=k * 2)
                members.append(aid)
                arts.append({
                    "id": aid, "url": f"https://pub{aid % 500}.test/{aid}", "source_id": rng.randint(1, 66), "story_id": sid,
                    "slug": f"article-{aid}", "raw_title": text(rng, 80), "title": text(rng, 80), "headline": text(rng, 90),
                    "author": "A. Writer", "domain": f"pub{aid % 500}.test", "published_at": ts(pub), "fetched_at": ts(pub),
                    "lang": "en", "description": text(rng, 280), "feed_content": text(rng, 1500), "content_md": text(rng, 8000),
                    "content_text": text(rng, 8000), "word_count": 1300, "image_url": f"https://img.test/{aid}.jpg",
                    "extraction_method": "trafilatura", "extraction_ok": 1, "show_fulltext": 1, "status": "published",
                    "reject_reason": None, "simhash": rng.randint(-2**62, 2**62), "summary_md": text(rng, 1000),
                    "key_points": json.dumps([text(rng, 150) for _ in range(3)]), "why_it_matters": text(rng, 300),
                    "category": rng.choice(["models", "agents", "business"]), "entities": json.dumps(ents),
                    "content_type": "news", "importance": rng.randint(3, 9), "enrich_model": "groq:qwen",
                    "embedding": json.dumps(emb), "predicted_score": rng.random(), "engagement": 0.0,
                    "model_release": None, "funding": None, "discussion_site": "hn" if k == 0 and rng.random() < 0.3 else None,
                    "discussion_url": None, "discussion_points": rng.randint(5, 400) if rng.random() < 0.3 else None,
                    "trend_score": rng.randint(0, 50) if rng.random() < 0.3 else None, "discussion_checked_at": None,
                    "created_at": ts(pub),
                })
            if sid % 4 == 1:
                tid += 1
                threads.append((tid, f"thread-{tid}", text(rng, 60), text(rng, 300), "models", json.dumps(ents), json.dumps(base), 1, 0,
                                 ts(first), ts(first), "published"))
            stories.append((sid, f"story-{sid}", text(rng, 90), text(rng, 1000), json.dumps([text(rng, 150) for _ in range(3)]), text(rng, 300),
                            "models", json.dumps(ents), members[0], n, rng.randint(3, 9), rng.random(), json.dumps(base), "published", 0,
                            tid if rng.random() < 0.6 else None, text(rng, 400) if rng.random() < 0.1 else None, None, None,
                            ts(first), ts((first + timedelta(hours=2 * (n - 1))))))
        for _ in range(rejected_per_day):
            aid += 1
            t = now - timedelta(days=d, minutes=rng.randint(0, 1439))
            arts.append({
                "id": aid, "url": f"https://rej{aid % 700}.test/{aid}", "source_id": rng.randint(1, 66), "story_id": None, "slug": None,
                "raw_title": text(rng, 80), "title": text(rng, 80), "headline": None, "author": None, "domain": f"rej{aid % 700}.test",
                "published_at": ts(t), "fetched_at": ts(t), "lang": "en", "description": text(rng, 280),
                "feed_content": text(rng, 1500), "content_md": text(rng, 8000), "content_text": text(rng, 8000), "word_count": 1300,
                "image_url": None, "extraction_method": "trafilatura", "extraction_ok": 1, "show_fulltext": 1, "status": "rejected",
                "reject_reason": rng.choice(["gate: not about AI (score 1.0)", "gate: duplicate title of #3", "llm: not ai news", "extract: failed"]),
                "simhash": rng.randint(-2**62, 2**62), "summary_md": None, "key_points": None, "why_it_matters": None, "category": None,
                "entities": None, "content_type": None, "importance": None, "enrich_model": None, "embedding": None, "predicted_score": None,
                "engagement": 0.0, "model_release": None, "funding": None, "discussion_site": None, "discussion_url": None,
                "discussion_points": None, "trend_score": None, "discussion_checked_at": None, "created_at": ts(t),
            })
    cols = list(arts[0])
    for i in range(0, len(arts), 2000):
        cur.executemany(f"insert into articles ({','.join(cols)}) values ({','.join('?' * len(cols))})", [tuple(a[c] for c in cols) for a in arts[i:i + 2000]])
    cur.executemany("insert into stories (id, slug, headline, summary_md, key_points, why_it_matters, category, entities, lead_article_id, article_count, importance, score, embedding, status, pinned, thread_id, pulse, pulse_at, pushed_at, first_published_at, updated_at) values (" + ",".join("?" * 21) + ")", stories)
    cur.executemany("insert into threads (id, slug, title, summary, category, entities, embedding, story_count, named_count, first_at, updated_at, status) values (" + ",".join("?" * 12) + ")", threads)
    # Two weeks of run history (one row per step), and reader events.
    runs = []
    for h in range(14 * 24):
        t = now - timedelta(hours=h)
        for step in ["fetch", "extract", "gate", "enrich", "cluster", "threads", "discuss", "pulse", "rank", "export", "push", "topics",
                     "intros", "images", "audio", "social", "newsletter", "gsc", "admin", "notify", "indexnow"]:
            runs.append((ts(t), ts((t + timedelta(seconds=20))), step, json.dumps({"items": 1200, "inserted": 40, "seconds": 12.5, "reasons": {"not about AI": 30, "duplicate title": 12}})))
    cur.executemany("insert into runs (started_at, finished_at, step, stats) values (?,?,?,?)", runs)
    events = []
    for _ in range(days * 400):
        a = rng.randint(1, aid)
        t = now - timedelta(minutes=rng.randint(0, days * 1440))
        events.append((a, None, rng.choice(["view", "view", "dwell", "click_source"]), rng.random() * 60, f"s{rng.randint(1, 5000)}", f"v{rng.randint(1, 3000)}", "direct", f"/story/story-{rng.randint(1, sid)}", None, ts(t)))
    cur.executemany("insert into events (article_id, story_id, type, value, session, visitor, source, path, detail, created_at) values (?,?,?,?,?,?,?,?,?,?)", events)
    con.commit()
    con.close()
    print(f"built {path}: {sid} stories, {aid} articles, {tid} threads, {os.path.getsize(path) / 1e6:.0f} MB")


def add_batch(path: Path, run_no: int, n_new: int = 25) -> None:
    """What arrives between runs: summarised-ready articles, a few discussion updates, reader events."""
    import sqlite3

    rng = random.Random(1000 + run_no)
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(path)
    base = con.execute("select coalesce(max(id), 0) from articles").fetchone()[0]
    rows = []
    for k in range(n_new):
        i = base + k + 1
        t = now - timedelta(minutes=rng.randint(0, 50))
        rows.append((f"https://new{run_no}.test/{i}", rng.randint(1, 66), text(rng, 80), text(rng, 80), f"new{i % 90}.test", ts(t), ts(t),
                     "OpenAI AI model launch. " + text(rng, 280), text(rng, 8000), "OpenAI releases a new AI model for agents. " * 20 + text(rng, 6000),
                     1300, 1, "extracted" if k % 3 == 0 else "gated", rng.randint(-2**62, 2**62), ts(t), "trafilatura", 1, 0.0))
    con.executemany("insert into articles (url, source_id, raw_title, title, domain, published_at, fetched_at, description, content_md, content_text, word_count, show_fulltext, status, simhash, created_at, extraction_method, extraction_ok, engagement) values (" + ",".join("?" * 18) + ")", rows)
    for a, pts in con.execute("select id, abs(random()) % 300 from articles where status = 'published' order by id desc limit 15").fetchall():
        con.execute("update articles set discussion_site = 'hn', discussion_url = ?, discussion_points = ? where id = ?", (f"https://news.ycombinator.com/item?id={a}", pts, a))
    for _ in range(300):
        a = con.execute("select id from articles where status = 'published' order by random() limit 1").fetchone()[0]
        con.execute("insert into events (article_id, type, value, session, visitor, source, path, created_at) values (?, 'view', 1, ?, ?, 'direct', '/', ?)",
                    (a, f"s{rng.randint(1, 5000)}", f"v{rng.randint(1, 3000)}", ts(now)))
    con.commit()
    con.close()


def patch_offline() -> None:
    import requests

    from digest import cluster

    def no_network(*a, **k):
        raise requests.ConnectionError("offline benchmark")

    requests.get = requests.head = requests.post = no_network
    requests.Session.get = requests.Session.post = requests.Session.head = no_network
    rng = random.Random(3)
    cluster._load_model = lambda: True
    cluster._MODEL_NAME = "bge-small-en-v1.5"
    cluster.embed = lambda texts: [unit(rng) for _ in texts]


def _quiet(fn):
    """run.main prints its summary as JSON; keep the table readable."""
    import contextlib
    import io

    def wrapped(*a, **k):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **k)
    return wrapped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--stories-per-day", type=int, default=100)
    ap.add_argument("--rejected-per-day", type=int, default=500)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--cold", action="store_true", help="start without the runner's cache")
    ap.add_argument("--queries", action="store_true", help="list the statements that read the most in each run")
    args = ap.parse_args()
    path = Path(args.db).resolve()
    os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OLLAMA_URL", "KIT_API_KEY", "VAPID_PRIVATE_KEY", "BLUESKY_APP_PASSWORD"):
        os.environ[k] = ""
    # The runner's copy of earlier reads (cache.py) lives next to the benchmark database.
    cache_dir = path.with_name(path.stem + "-cache")
    os.environ["CACHE_DIR"] = str(cache_dir)
    sys.path.insert(0, str(HERE))
    if args.rebuild and path.exists():
        path.unlink()
    fresh = not path.exists()
    if fresh or args.cold:
        import shutil

        shutil.rmtree(cache_dir, ignore_errors=True)
    per_query: dict[str, float] = {}
    if args.queries:
        # Attribute the bytes of each fetched row to the statement that produced it.
        from sqlalchemy import event

        from digest import db as dbmod

        current = {"sql": ""}
        original = dbmod._count_rows

        def counting(cursor, row):
            before = dbmod._bytes_read[0]
            original(cursor, row)
            per_query[current["sql"]] = per_query.get(current["sql"], 0) + dbmod._bytes_read[0] - before
            return row

        dbmod._count_rows = counting
        real_engine = dbmod.engine

        def engine():
            new = dbmod._engine is None
            eng = real_engine()
            if new:
                event.listen(eng, "before_cursor_execute", lambda c, cur, stmt, *a: current.update(sql=" ".join(stmt.split())[:150]))
            return eng

        dbmod.engine = engine
    if fresh:
        build(path, args.days, args.stories_per_day, args.rejected_per_day)
    patch_offline()
    import logging

    from digest import db, run

    logging.disable(logging.WARNING)
    steps = [s for s in args.steps.split(",") if s in run.STEPS]
    table: dict[str, list] = {s: [] for s in steps}
    run.main = _quiet(run.main)
    for r in range(args.runs):
        add_batch(path, r)
        from sqlalchemy import func, select

        with db.engine().connect() as conn:
            last_id = conn.execute(select(func.max(db.runs.c.id))).scalar() or 0
        t0 = time.time()
        run.main(steps)
        with db.engine().connect() as conn:
            rows = conn.execute(select(db.runs.c.step, db.runs.c.stats).where(db.runs.c.id > last_id).order_by(db.runs.c.id)).all()
        for step, stats in rows:
            table.setdefault(step, []).append((stats or {}).get("readKB"))
        print(f"run {r + 1}: {time.time() - t0:.0f} s", file=sys.stderr)
        if args.queries:
            print(f"\nrun {r + 1}: biggest queries")
            for sql, n in sorted(per_query.items(), key=lambda kv: -kv[1])[:12]:
                print(f"{n / 1024:>9.1f} KB  {sql}")
            per_query.clear()
    print("\nstep        " + "".join(f"run{i + 1:>2} KB  " for i in range(args.runs)))
    totals = [0.0] * args.runs
    for step, vals in table.items():
        print(f"{step:<12}" + "".join(f"{(v if v is not None else float('nan')):>9.1f}  " for v in vals))
        for i, v in enumerate(vals[: args.runs]):
            totals[i] += v or 0
    print(f"{'total':<12}" + "".join(f"{v:>9.1f}  " for v in totals))
    print(f"{'x 48 x 31':<12}" + "".join(f"{v * 48 * 31 / 1024 / 1024:>7.2f}GB  " for v in totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
