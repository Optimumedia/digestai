"""One-time copy of the pipeline's SQLite database into Supabase Postgres.

usage (repo root):  .venv\\Scripts\\python scripts\\migrate_sqlite_to_postgres.py path\\to\\digest.db
DATABASE_URL (Postgres) comes from .env. Existing rows with the same ids are skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from sqlalchemy import create_engine, select, text  # noqa: E402

from digest import config, db  # noqa: E402

ORDER = ["sources", "stories", "articles", "threads", "llm_usage", "newsletters", "runs", "events"]


def main(path: str) -> int:
    if config.DATABASE_URL.startswith("sqlite"):
        print("DATABASE_URL must point at Postgres (Supabase).")
        return 1
    src = create_engine(f"sqlite:///{Path(path).resolve().as_posix()}", future=True)
    dst = db.engine()
    copied = {}
    with src.connect() as s, dst.begin() as d:
        for name in ORDER:
            table = db.metadata.tables[name]
            existing = {r[0] for r in d.execute(select(table.c.id)).all()}
            rows = [dict(r._mapping) for r in s.execute(select(table)).all()]
            new = [r for r in rows if r["id"] not in existing]
            for i in range(0, len(new), 500):
                d.execute(table.insert(), new[i : i + 500])
            copied[name] = len(new)
            # Keep the id sequence ahead of the copied ids.
            d.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), COALESCE((SELECT MAX(id) FROM {name}), 1))"))
    print("copied:", copied)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "pipeline" / "data" / "digest.db")))
