"""Apply supabase/schema.sql to the database in DATABASE_URL (from .env or the environment).

Run from the repo root:  .venv\\Scripts\\python scripts\\apply_schema.py
Safe to run repeatedly; every statement in schema.sql is idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from digest import config, db  # noqa: E402


def main() -> int:
    if config.DATABASE_URL.startswith("sqlite"):
        print("DATABASE_URL is not set to a Postgres/Supabase connection; nothing to do.")
        return 1
    eng = db.engine()  # creates any missing tables first
    sql = (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8")
    sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    # Split on semicolons, but not inside $$ ... $$ function bodies.
    statements, buf, in_dollar = [], "", False
    for ch in sql:
        buf += ch
        if buf.endswith("$$"):
            in_dollar = not in_dollar
        if ch == ";" and not in_dollar:
            statements.append(buf.strip().rstrip(";").strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip().rstrip(";").strip())
    from sqlalchemy import text

    applied = 0
    with eng.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))  # never queue behind a running pipeline
        for body in statements:
            if not body:
                continue
            conn.execute(text(body))
            applied += 1
    print(f"applied {applied} statements to {eng.url.host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
