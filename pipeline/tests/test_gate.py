"""The gate in the events guard: which reader events the database stores, which it drops, and what
it counts. Offline the rules are checked as text: python tests/test_gate.py. With TEST_POSTGRES_URL
(the postgres-tests workflow) the guard itself runs on a real Postgres, with the request headers the
API would set."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, insert, select, text  # noqa: E402

from digest import db  # noqa: E402

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
LINKEDIN = SAFARI.replace("Safari/604.1", "[LinkedInApp]/9.31.1234")
CUBOT = "Mozilla/5.0 (Linux; Android 13; CUBOT KINGKONG 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
CUBOT_X = "Mozilla/5.0 (Linux; Android 12; CUBOT_X30) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
READERS = [CHROME, SAFARI, LINKEDIN, CUBOT, CUBOT_X]
PROGRAMS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (Linux; Android 7.0;) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; PetalBot;+https://webmaster.petalsearch.com/site/petalbot)",
    "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)",
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
    "python-requests/2.32.3",
    "curl/8.9.1",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Chrome-Lighthouse",
]


def test_the_rules_as_text():
    sql = db.EVENTS_GUARD_SQL
    assert "__" not in sql  # every placeholder was filled in
    assert "%" not in sql and "\\" not in sql  # neither survives the driver or a copy into schema.sql
    assert chr(8) not in sql
    for origin in db.GATE_ORIGINS:
        assert f"'{origin}'" in sql
    assert "return null;" in sql  # a dropped event is skipped, not refused: the sender learns nothing
    assert sql.count("exception when others then") == 2  # reading the headers, and writing the count
    # Until a day of real counts shows the API passes Origin and User-Agent on, a request without
    # them is counted apart and stored.
    assert db.GATE_STRICT is False and "firm boolean := false;" in sql
    assert "firm boolean := true;" in db.events_guard_sql(True)
    assert db.events_guard_sql(True).replace(":= true;", ":= false;") == sql
    assert set(re.findall(r"verdict := '([a-z]+)'", sql)) == set(db.GATE_REASONS)
    # The count never holds an address: the only request headers read are these three.
    assert set(re.findall(r"hdr->>'([a-z-]+)'", sql)) == {"origin", "user-agent", "cf-ipcountry"}
    assert [c.name for c in db.event_gate.columns] == ["day", "country", "reason", "n"]
    # The copy for a new database is word for word the one the pipeline installs.
    schema = (Path(__file__).resolve().parents[2] / "supabase" / "schema.sql").read_text(encoding="utf-8")
    assert sql in schema


def test_the_name_rules_tell_readers_from_programs():
    """The same two patterns the guard uses, on real browser names."""
    named = re.compile(f"({db.GATE_PROGRAMS})")
    crawler = re.compile(db.GATE_CRAWLERS)
    is_program = lambda ua: bool(named.search(ua.lower()) or crawler.search(ua.lower()))  # noqa: E731
    for ua in READERS:
        assert not is_program(ua), ua
    for ua in PROGRAMS:
        assert is_program(ua), ua


def _send(eng, headers, **event) -> int:
    """One insert as the public API makes it: the request headers set for the transaction, then the
    row. Returns how many rows were stored."""
    row = {"type": "view", "value": 1.0, "session": "s1", "visitor": "v1", "path": "/", "created_at": datetime.now(timezone.utc)}
    row.update(event)
    with eng.begin() as conn:
        if headers is not None:
            raw = headers if isinstance(headers, str) else json.dumps(headers)
            conn.execute(text("select set_config('request.headers', :h, true)"), {"h": raw})
        before = conn.execute(select(func.count()).select_from(db.events)).scalar()
        conn.execute(insert(db.events).values(**row))
        return conn.execute(select(func.count()).select_from(db.events)).scalar() - before


def _gate(eng) -> dict:
    with eng.connect() as conn:
        return {(r.country, r.reason): r.n for r in conn.execute(select(db.event_gate)).all()}


def _headers(ua=CHROME, origin="https://digestai.news", country="DE") -> dict:
    out = {"user-agent": ua, "x-forwarded-for": "203.0.113.9"}
    if origin is not None:
        out["origin"] = origin
    if country is not None:
        out["cf-ipcountry"] = country
    return out


def _fresh(strict: bool):
    """An empty Postgres with the pipeline's tables and the guard installed as supabase/schema.sql
    installs it, or None when no Postgres is at hand."""
    pg = os.environ.get("TEST_POSTGRES_URL")
    if not pg:
        print("  (no TEST_POSTGRES_URL: the guard itself runs in the postgres-tests workflow)")
        return None
    eng = create_engine(pg, future=True)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    db.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text(db.events_guard_sql(strict)))
        conn.execute(text("create trigger events_guard before insert on events for each row execute function public.events_guard()"))
    return eng


def test_the_guard_on_postgres():
    eng = _fresh(strict=db.GATE_STRICT)
    if eng is None:
        return
    try:
        # The pipeline's own inserts carry no request headers: stored, and nothing counted.
        assert _send(eng, None) == 1
        assert _gate(eng) == {}

        # A reader on our page: stored, and the view counted under the network's country.
        assert _send(eng, _headers()) == 1
        assert _send(eng, _headers(origin="https://www.digestai.news")) == 1
        assert _send(eng, _headers(origin="https://digestai-news.translate.goog")) == 1
        assert _gate(eng) == {("DE", "ok"): 3}
        # Only views are counted, so the total reads as "views by country".
        assert _send(eng, _headers(), type="dwell", value=30.0) == 1
        assert _gate(eng) == {("DE", "ok"): 3}
        for ua in READERS:
            assert _send(eng, _headers(ua=ua, country="PL")) == 1, ua
        assert _gate(eng)[("PL", "ok")] == len(READERS)

        # Sent from a page that is not ours: dropped without an error, and counted.
        elsewhere = ("https://evil.example", "http://localhost:4321", "null", "https://digestai.news.evil.example")
        for origin in elsewhere:
            assert _send(eng, _headers(origin=origin, country="CN")) == 0, origin
        assert _gate(eng)[("CN", "origin")] == len(elsewhere)
        # Every kind of event from there is dropped, not only views.
        assert _send(eng, _headers(origin="https://evil.example", country="CN"), type="dwell", value=30.0) == 0
        assert _gate(eng)[("CN", "origin")] == len(elsewhere) + 1

        # From our page, but a program that names itself.
        for ua in PROGRAMS:
            assert _send(eng, _headers(ua=ua, country="SG")) == 0, ua
        assert _gate(eng)[("SG", "client")] == len(PROGRAMS)
        # A script with no Origin that names itself is a program first.
        assert _send(eng, _headers(ua="curl/8.9.1", origin=None, country="SG")) == 0
        assert _gate(eng)[("SG", "client")] == len(PROGRAMS) + 1

        # No Origin or no User-Agent at all: counted apart and, for now, stored.
        assert _send(eng, _headers(origin=None, country="US")) == 1
        assert _send(eng, _headers(origin="", country="US")) == 1
        assert _send(eng, _headers(ua="", country="US")) == 1
        gate = _gate(eng)
        assert gate[("US", "noorigin")] == 2 and gate[("US", "noagent")] == 1 and ("US", "ok") not in gate

        # A country the network did not report, or reported oddly, is ZZ.
        assert _send(eng, _headers(country=None)) == 1
        assert _send(eng, _headers(country="T1x")) == 1
        assert _send(eng, _headers(country="xx")) == 1
        gate = _gate(eng)
        assert gate[("ZZ", "ok")] == 2 and gate[("XX", "ok")] == 1

        # Headers that cannot be read never cost a reader's event.
        assert _send(eng, "not json at all") == 1
        assert _send(eng, "") == 1
        assert _send(eng, "[1, 2]") == 1
        assert set(r for _, r in _gate(eng)) <= set(db.GATE_REASONS)

        # The older rules still hold behind the gate.
        try:
            _send(eng, _headers(), story_id=987654)
            raise AssertionError("an unknown story was stored")
        except Exception as exc:  # noqa: BLE001
            assert "unknown story" in str(exc), exc
        before = _gate(eng)[("DE", "ok")]
        for _ in range(29):
            _send(eng, _headers(), session="flood", type="dwell")
        assert _send(eng, _headers(), session="flood") == 1  # the 30th event of the minute
        try:
            _send(eng, _headers(), session="flood")
            raise AssertionError("a flooding session was stored")
        except Exception as exc:  # noqa: BLE001
            assert "too many events" in str(exc), exc
        assert _gate(eng)[("DE", "ok")] == before + 1  # the refused view was not counted

        # Without the count table the gate still decides, and readers are still stored.
        with eng.begin() as conn:
            conn.execute(text("drop table event_gate"))
        assert _send(eng, _headers(), session="s2") == 1
        assert _send(eng, _headers(ua="curl/8.9.1"), session="s2") == 0
    finally:
        eng.dispose()


def test_the_strict_guard_on_postgres():
    """What GATE_STRICT turns on: a request with no Origin or no User-Agent is dropped too."""
    eng = _fresh(strict=True)
    if eng is None:
        return
    try:
        assert _send(eng, None) == 1  # the pipeline's own inserts are never gated
        assert _send(eng, _headers()) == 1
        assert _send(eng, _headers(origin=None, country="US")) == 0
        assert _send(eng, _headers(ua="", country="US")) == 0
        assert _send(eng, _headers(ua="", country="US"), type="dwell") == 0
        assert _send(eng, "not json at all") == 1
        assert _gate(eng) == {("DE", "ok"): 1, ("US", "noorigin"): 1, ("US", "noagent"): 2}
    finally:
        eng.dispose()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
