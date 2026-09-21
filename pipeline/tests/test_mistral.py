"""Mistral as a summary provider: where it stands in each step's order, the monthly spend cap, the
month rollover, the plan-level pause and what is recorded.

Offline, with a fake HTTP session, on a temporary SQLite database (or on Postgres with
TEST_POSTGRES_URL, as the postgres-tests workflow does): python tests/test_mistral.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, insert, select  # noqa: E402

from digest import admin, cache, config, db, enrich, howto, morning, upgrade  # noqa: E402

# No test here reaches a real provider: keys in .env must not make Mistral or Cloudflare live (the
# tests patch in test keys and a fake HTTP session).
config.MISTRAL_API_KEY = ""
config.CLOUDFLARE_ACCOUNT_ID = config.CLOUDFLARE_AI_TOKEN = ""

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
ANSWER = ('{"headline": "Acme ships a model", "summary_md": "Acme released a model on Monday.", '
          '"key_points": ["Acme released a model"], "why_it_matters": "It matters.", "category": "models", '
          '"entities": {"companies": ["Acme"], "models": [], "people": []}, "content_type": "news", '
          '"importance": 5, "is_ai_news": true, "model_release": null, "funding": null, "work_card": null}')


@contextmanager
def fresh_db():
    tmp = Path(tempfile.mkdtemp())
    saved = (db._engine, config.CACHE_DIR, config.SITE_DATA_DIR)
    pg = os.environ.get("TEST_POSTGRES_URL")  # set by .github/workflows/postgres-tests.yml
    if pg:
        eng = create_engine(pg, future=True)
        with eng.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
        db._tracking_ready.clear()
    else:
        eng = create_engine(f"sqlite:///{(tmp / 'test.db').as_posix()}", future=True)
    db.install_read_meter(eng)
    db.metadata.create_all(eng)
    db.install_change_tracking(eng)
    db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = eng, tmp / "cache", tmp / "site"
    cache.reset()
    enrich.mistral_reset()
    enrich.cloudflare_reset()
    try:
        yield eng
    finally:
        db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = saved
        cache.reset()
        enrich.mistral_reset()
        enrich.cloudflare_reset()
        eng.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


class Patch:
    """Set attributes on modules and put them back afterwards."""

    def __init__(self, *items):
        self.items = items  # (module, name, value)

    def __enter__(self):
        self.saved = [(m, n, getattr(m, n)) for m, n, _v in self.items]
        for m, n, v in self.items:
            setattr(m, n, v)
        return self

    def __exit__(self, *exc):
        for m, n, old in self.saved:
            setattr(m, n, old)
        return False


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = str(body)[:200]

    def json(self):
        return self._body


class FakeMistral:
    """Stands in for requests.post: answers like Mistral's chat completions endpoint."""

    def __init__(self, status=200, headers=None, prompt_tokens=8000, completion_tokens=1000, content=ANSWER):
        self.status, self.headers = status, headers or {}
        self.prompt_tokens, self.completion_tokens, self.content = prompt_tokens, completion_tokens, content
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        assert url == "https://api.mistral.ai/v1/chat/completions", url
        assert headers["Authorization"] == "Bearer test-key"
        self.calls.append(json)
        if self.status != 200:
            return Resp(self.status, {"message": "limit"}, self.headers)
        return Resp(200, {"choices": [{"message": {"content": self.content}}],
                          "usage": {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}})


def keyed(*extra):
    """Mistral configured with a test key and the default price, cap and model."""
    return Patch((config, "MISTRAL_API_KEY", "test-key"), (config, "MISTRAL_URL", "https://api.mistral.ai/v1"),
                 (config, "MISTRAL_MODEL", "ministral-8b-latest"), (config, "MISTRAL_MONTHLY_CAP_USD", 9.0),
                 (config, "MISTRAL_PRICE_IN_PER_M", 0.15), (config, "MISTRAL_PRICE_OUT_PER_M", 0.15),
                 (config, "MISTRAL_MAX_PER_RUN", 60), (db, "utcnow", lambda: NOW), *extra)


def spend_row(eng, day: str, micro: int, provider: str = enrich.MISTRAL_SPEND):
    with eng.begin() as conn:
        conn.execute(insert(db.llm_usage).values(day=day, provider=provider, requests=micro, exhausted=False))


def usage_rows(eng) -> dict[tuple[str, str], int]:
    with eng.connect() as conn:
        return {(r.day, r.provider): r.requests for r in conn.execute(select(db.llm_usage)).all()}


# ---------------------------------------------------------------------------- order

def seed_articles(eng, n=2):
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="press", name="Press", url="https://press.test/feed",
                                               source_type="press", weight=1.0))
        for i in range(1, n + 1):
            conn.execute(insert(db.articles).values(
                id=i, url=f"https://press.test/{i}", source_id=1, title=f"Acme ships a model {i}", domain="press.test",
                published_at=NOW - timedelta(hours=i), created_at=NOW - timedelta(hours=i), fetched_at=NOW,
                status="gated", content_text="Acme released a model on Monday. " * 60, description="d"))


def test_enrich_order_is_gemini_cloud_groq_cloudflare_mistral_then_local():
    with fresh_db() as eng:
        seed_articles(eng, 2)
        tried: list[str] = []

        def refusing(name):
            def call(prompt):
                tried.append(name)
                raise enrich.QuotaExhausted(f"{name} out for the day")
            return call

        def local(prompt):
            tried.append("ollama")
            return enrich._parse_json(ANSWER)

        # The first article: the four free providers refuse and Mistral answers. The second: Mistral
        # answers 402 (out of money), so the local model, last in line, takes it.
        fake = FakeMistral()
        prompts: list[str] = []

        def post(url, headers=None, json=None, timeout=None):  # noqa: A002
            prompts.append(json["messages"][0]["content"])
            tried.append("mistral")
            if len(prompts) == 2:
                return Resp(402, {"message": "payment required"})
            return fake(url, headers=headers, json=json, timeout=timeout)

        with keyed((config, "GEMINI_API_KEY", "k"), (config, "OLLAMA_API_KEY", "k"), (config, "GROQ_API_KEY", "k"),
                   (config, "CHECK_SUMMARIES", False),
                   (enrich, "allowance", lambda conn, p: 5), (enrich, "ollama_available", lambda: True),
                   (enrich, "call_gemini", refusing("gemini")), (enrich, "call_ollama_cloud", refusing("cloud")),
                   (enrich, "call_groq", refusing("groq")), (enrich, "call_ollama", local),
                   (config, "CLOUDFLARE_ACCOUNT_ID", "acct"), (config, "CLOUDFLARE_AI_TOKEN", "tok"),
                   (enrich, "cloudflare_allowance", lambda conn=None: 5), (enrich, "call_cloudflare", refusing("cloudflare")),
                   (enrich.requests, "post", post), (enrich.time, "sleep", lambda s: None)):
            stats = enrich.run()
        assert tried == ["gemini", "cloud", "groq", "cloudflare", "mistral", "mistral", "ollama"], tried
        assert stats["enriched"] == 2, stats
        # Mistral gets the full prompt (worked examples, importance ladder), not the trimmed local one.
        assert "Example 1" in prompts[0] and "8-9 = news a professional" in prompts[0]
        with eng.connect() as conn:
            models = dict(conn.execute(select(db.articles.c.id, db.articles.c.enrich_model)).all())
        assert sorted(models.values()) == ["mistral:ministral-8b-latest", f"ollama:{config.OLLAMA_MODEL}"], models


def test_upgrade_and_howto_put_mistral_after_their_providers():
    with fresh_db() as eng:
        with keyed((config, "GEMINI_API_KEY", "k"), (config, "OLLAMA_API_KEY", "k"),
                   (config, "STRONG_PROVIDERS", ["gemini", "cloud"]), (enrich, "spare", lambda conn, p: 5),
                   (enrich, "allowance", lambda conn, p: 5)):
            with eng.connect() as conn:
                names = [n.split(":")[0] for n, _fn in upgrade._providers(conn)]
            assert names == ["gemini", "cloud", "mistral"], names
            # Out of money: no longer offered.
            spend_row(eng, NOW.date().isoformat(), 9_000_000)
            enrich.mistral_reset()
            with eng.connect() as conn:
                assert [n.split(":")[0] for n, _fn in upgrade._providers(conn)] == ["gemini", "cloud"]

    tried: list[str] = []

    def refusing(name):
        def call(prompt):
            tried.append(name)
            raise RuntimeError(f"{name} down")
        return call

    def mistral(prompt):
        tried.append("mistral")
        return {"steps": ["Open the tool"]}

    with Patch((config, "GEMINI_API_KEY", "k"), (config, "GROQ_API_KEY", "k"), (config, "OLLAMA_API_KEY", "k"),
               (config, "MISTRAL_API_KEY", "k"), (enrich, "call_gemini", refusing("gemini")),
               (enrich, "call_groq", refusing("groq")), (enrich, "call_ollama_cloud", refusing("cloud")),
               (config, "CLOUDFLARE_ACCOUNT_ID", "acct"), (config, "CLOUDFLARE_AI_TOKEN", "tok"),
               (enrich, "call_cloudflare", refusing("cloudflare")), (enrich, "call_mistral", mistral)):
        assert howto.ask_model("prompt") == {"steps": ["Open the tool"]}
    # simplify.py asks through howto.ask_model, so it gets the same order.
    assert tried == ["gemini", "groq", "cloud", "cloudflare", "mistral"], tried


# ---------------------------------------------------------------------------- the cap

def test_cap_stops_calls_at_nine_dollars():
    with fresh_db() as eng:
        today = NOW.date().isoformat()
        # $8.9997 spent: a call's worst case (its prompt plus a full 3,000-token answer, ~$0.0005)
        # would pass $9.00, so it is not sent.
        spend_row(eng, today, 8_999_700)
        fake = FakeMistral()
        with keyed((enrich.requests, "post", fake)):
            try:
                enrich.call_mistral("Summarise this.")
                raise AssertionError("a call past the cap must be refused")
            except enrich.SpendCapReached as exc:
                assert "monthly cap" in str(exc)
            assert fake.calls == [], "the refusal happens before anything is sent"
            with eng.connect() as conn:
                assert enrich.mistral_allowance(conn) == 0
    with fresh_db() as eng:
        spend_row(eng, NOW.date().replace(day=2).isoformat(), 8_990_000)  # $8.99 earlier this month
        # Each answer costs $0.01 (66,667 tokens at $0.15 per million): one more call fits, then none.
        fake = FakeMistral(prompt_tokens=60_000, completion_tokens=6_667)
        with keyed((enrich.requests, "post", fake)):
            assert enrich.call_mistral("Summarise this.")["headline"] == "Acme ships a model"
            assert abs(enrich.mistral_spent_usd() - 9.0) < 1e-6, enrich.mistral_spent_usd()
            try:
                enrich.call_mistral("Summarise this.")
                raise AssertionError("the running total must stop the next call")
            except enrich.SpendCapReached:
                pass
            assert len(fake.calls) == 1
            # A new run (a new process) reads the same total back from llm_usage.
            enrich.mistral_reset()
            try:
                enrich.call_mistral("Summarise this.")
                raise AssertionError("the stored total must stop the next run too")
            except enrich.SpendCapReached:
                pass


def test_run_cap_limits_calls_per_run():
    with fresh_db():
        fake = FakeMistral()
        with keyed((enrich.requests, "post", fake), (config, "MISTRAL_MAX_PER_RUN", 3)):
            for _ in range(3):
                enrich.call_mistral("p")
            try:
                enrich.call_mistral("p")
                raise AssertionError("the run's call cap must hold")
            except enrich.ProviderPaused:
                pass
            assert len(fake.calls) == 3 and enrich.mistral_allowance() == 0


def test_month_rollover_resets_the_total():
    with fresh_db() as eng:
        spend_row(eng, "2026-08-30", 5_000_000)
        spend_row(eng, "2026-08-31", 4_000_000, "mistral_microusd")
        spend_row(eng, "2026-08-31", 250, "mistral")
        late = datetime(2026, 8, 31, 23, 58, tzinfo=timezone.utc)
        clock = {"now": late}
        fake = FakeMistral()
        with keyed((db, "utcnow", lambda: clock["now"]), (enrich.requests, "post", fake)):
            assert enrich.mistral_block() and "monthly cap" in enrich.mistral_block()
            # The same run crosses midnight into the 1st (UTC): the new month starts from zero.
            clock["now"] = datetime(2026, 9, 1, 0, 3, tzinfo=timezone.utc)
            assert enrich.mistral_block() is None
            enrich.call_mistral("p")
            with eng.connect() as conn:
                u = enrich.mistral_usage(conn)
            assert u["month"] == "2026-09" and u["callsMonth"] == 1 and u["callsToday"] == 1
            assert u["spentMicro"] == round(enrich.mistral_cost(8000, 1000) * 1e6), u


# ---------------------------------------------------------------------------- limits from Mistral

def test_model_not_on_plan_pauses_for_24_hours():
    with fresh_db() as eng:
        clock = {"now": NOW}
        blocked = FakeMistral(status=429, headers={"x-ratelimit-limit-req-minute": "0"})
        with keyed((db, "utcnow", lambda: clock["now"]), (enrich.requests, "post", blocked)):
            try:
                enrich.call_mistral("p")
                raise AssertionError("a model that is not on the plan must be reported")
            except enrich.QuotaExhausted as exc:
                assert "not available on this plan" in str(exc)
            assert len(blocked.calls) == 1
            # Later runs (new processes) still see the pause, stored in llm_usage, for 24 hours.
            for hours in (1, 12, 23.9):
                enrich.mistral_reset()
                clock["now"] = NOW + timedelta(hours=hours)
                assert "not available on this plan" in (enrich.mistral_block() or ""), hours
            enrich.mistral_reset()
            clock["now"] = NOW + timedelta(hours=24, minutes=1)
            assert enrich.mistral_block() is None
        assert len(blocked.calls) == 1


def test_rate_limit_or_payment_pauses_for_the_run_only():
    for status in (429, 402):
        with fresh_db():
            busy = FakeMistral(status=status, headers={"x-ratelimit-limit-req-minute": "188"})
            with keyed((enrich.requests, "post", busy)):
                try:
                    enrich.call_mistral("p")
                    raise AssertionError("a limit must pause Mistral")
                except enrich.ProviderPaused:
                    pass
                try:
                    enrich.call_mistral("p")
                except enrich.ProviderPaused as exc:
                    assert "paused for this run" in str(exc)
                assert len(busy.calls) == 1, "nothing more is sent in this run"
                enrich.mistral_reset()  # the next run tries again
                assert enrich.mistral_block() is None


# ---------------------------------------------------------------------------- what is recorded

def test_usage_and_spend_are_recorded_once():
    with fresh_db() as eng:
        fake = FakeMistral(prompt_tokens=9_000, completion_tokens=1_200)
        with keyed((enrich.requests, "post", fake)):
            enrich.call_mistral("p")
            enrich.call_mistral("p")
            # Callers that count their providers (enrich, upgrade) must not count Mistral twice.
            enrich.record_usage(eng, "mistral", 1)
            rows = usage_rows(eng)
            today = NOW.date().isoformat()
            cost = enrich.mistral_cost(9_000, 1_200)
            assert rows[(today, "mistral")] == 2, rows
            assert rows[(today, enrich.MISTRAL_SPEND)] == 2 * round(cost * 1e6), rows
            # The request sent: the model, JSON mode, an answer ceiling.
            assert fake.calls[0]["model"] == "ministral-8b-latest"
            assert fake.calls[0]["response_format"] == {"type": "json_object"}
            # The dashboard's figures and line, and the admin read stays tiny.
            spend_row(eng, NOW.date().replace(day=1).isoformat(), 1_836_000)
            before = db.bytes_read()
            with eng.connect() as conn:
                card = admin.mistral_card(conn)
            assert (db.bytes_read() - before) < 2048
            assert card["callsToday"] == 2 and card["callsMonth"] == 2 and card["capUSD"] == 9.0
            assert card["line"] == f"Mistral: 2 calls today (2 this month) · ${card['spentUSD']:.2f} of $9.00 this month", card
            assert abs(card["spentUSD"] - (1.836 + 2 * round(cost * 1e6) / 1e6)) < 1e-4  # shown to 4 places


def test_morning_note_names_mistral_only_past_80_percent():
    base = {"quotaMB": 5120, "projectedMB": None, "readMB": 12.5, "runs": 24, "monthlyMB": 380, "exhausted": []}
    for spent, named in ((4.10, False), (7.19, False), (7.20, True), (8.999, True)):
        admin_json = {"llm": {"budgets": {}, "usage": [], "mistral": {"spentUSD": spent, "capUSD": 9.0}}}
        b = {**base, "mistral": morning.budget_facts(admin_json, NOW)["mistral"]}
        sentence = morning.s_budget(b)
        assert ("Mistral" in sentence) is named, (spent, sentence)
        assert sentence.endswith(".") and sentence.count(".") >= 1
    b = {**base, "mistral": {"spentUSD": 7.5, "capUSD": 9.0}}
    assert morning.s_budget(b).endswith("; Mistral has spent $7.50 of its $9.00 monthly cap."), morning.s_budget(b)
    b = {**base, "mistral": {"spentUSD": 9.0, "capUSD": 9.0}}
    assert "stays off until the 1st" in morning.s_budget(b)


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, type(exc).__name__, exc)
    sys.exit(1 if failures else 0)
