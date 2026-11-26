"""Summary throughput on the free models: Gemini's quota day and its per-minute limits, Groq's
rate-limit waits and pacing, the token-saving request settings, Mistral's missing plan, and the
local model working beside the API providers instead of after them.

Offline, with fake providers and a fake HTTP session, on a temporary SQLite database:
python tests/test_throughput.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from sqlalchemy import create_engine, insert, select  # noqa: E402

from digest import cache, config, db, enrich  # noqa: E402

# No test here reaches a real provider.
config.MISTRAL_API_KEY = ""
config.CLOUDFLARE_ACCOUNT_ID = config.CLOUDFLARE_AI_TOKEN = ""
config.OPENROUTER_API_KEY = ""
config.GEMINI_API_KEY = config.GROQ_API_KEY = config.OLLAMA_API_KEY = ""

WINTER = datetime(2026, 1, 15, tzinfo=timezone.utc)
SUMMER = datetime(2026, 7, 15, tzinfo=timezone.utc)
ANSWER = {"headline": "Acme ships a model", "summary_md": "Acme released a model on Monday.",
          "key_points": ["Acme released a model"], "why_it_matters": "It matters.", "category": "models",
          "entities": {"companies": ["Acme"], "models": [], "people": []}, "content_type": "news",
          "importance": 5, "is_ai_news": True, "model_release": None, "funding": None, "work_card": None}


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


@contextmanager
def fresh_db():
    tmp = Path(tempfile.mkdtemp())
    saved = (db._engine, config.CACHE_DIR, config.SITE_DATA_DIR)
    eng = create_engine(f"sqlite:///{(tmp / 'test.db').as_posix()}", future=True)
    db.install_read_meter(eng)
    db.metadata.create_all(eng)
    db.install_change_tracking(eng)
    db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = eng, tmp / "cache", tmp / "site"
    cache.reset()
    enrich.mistral_reset()
    enrich.gemini_reset()
    try:
        yield eng
    finally:
        db._engine, config.CACHE_DIR, config.SITE_DATA_DIR = saved
        cache.reset()
        enrich.mistral_reset()
        enrich.gemini_reset()
        eng.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = str(body)[:400]

    def json(self):
        return self._body


def gemini_429(quota_id: str, delay: str = "41s"):
    return Resp(429, {"error": {"code": 429, "message": "You exceeded your current quota.", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": quota_id, "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


def gemini_ok():
    import json
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(ANSWER)}]}, "finishReason": "STOP"}]})


GEMINI_MODELS = ["g-flash", "g-flash-2", "g-lite"]


def gemini_keyed(post):
    return Patch((config, "GEMINI_API_KEY", "k"), (config, "GEMINI_MODEL", GEMINI_MODELS[0]),
                 (config, "GEMINI_FALLBACK_MODELS", GEMINI_MODELS[1:]), (enrich.requests, "post", post))


def model_of(url: str) -> str:
    return url.rsplit("/", 1)[1].split(":")[0]


# ---------------------------------------------------------------------------- Gemini

def test_gemini_429_says_whether_the_day_or_the_minute_is_out():
    kind, wait, ids = enrich.gemini_quota(gemini_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier")._body)
    assert kind == "day" and "PerDay" in ids, (kind, ids)
    kind, wait, _ = enrich.gemini_quota(gemini_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "17s")._body)
    assert kind == "minute" and wait == 17.0, (kind, wait)
    # Nothing named: a wait, never the end of the day.
    assert enrich.gemini_quota({"error": {"message": "Please retry in 9.5s."}})[:2] == ("minute", 9.5)


def test_a_per_minute_limit_moves_on_without_ending_the_day():
    with fresh_db():
        calls = []

        def post(url, headers=None, json=None, timeout=None):  # noqa: A002
            calls.append(model_of(url))
            if model_of(url) == "g-flash":
                return gemini_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "30s")
            return gemini_ok()

        with gemini_keyed(post):
            assert enrich.call_gemini("p")["headline"] == "Acme ships a model"
            assert calls == ["g-flash", "g-flash-2"], calls
            # The limited model waits out its window: the next article goes straight to the next one.
            enrich.call_gemini("p")
            assert calls == ["g-flash", "g-flash-2", "g-flash-2"], calls
            assert "g-flash" not in enrich._gemini_dead


def test_only_every_model_out_for_the_day_ends_geminis_day():
    with fresh_db():
        def per_day(url, headers=None, json=None, timeout=None):  # noqa: A002
            return gemini_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier")

        with gemini_keyed(per_day):
            try:
                enrich.call_gemini("p")
                raise AssertionError("expected QuotaExhausted")
            except enrich.QuotaExhausted:
                pass
        enrich.gemini_reset()

        def per_minute(url, headers=None, json=None, timeout=None):  # noqa: A002
            return gemini_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")

        with gemini_keyed(per_minute):
            try:
                enrich.call_gemini("p")
                raise AssertionError("expected a wait")
            except enrich.QuotaExhausted:
                raise AssertionError("a per-minute limit must not end the day")
            except RuntimeError as exc:
                assert "per-minute" in str(exc) or "rate limited" in str(exc), exc


def test_hangs_cost_at_most_two_timeouts_a_run():
    with fresh_db():
        calls = []

        def hang(url, headers=None, json=None, timeout=None):  # noqa: A002
            calls.append(model_of(url))
            assert timeout <= enrich.GEMINI_TIMEOUT_SECONDS
            raise requests.ReadTimeout("read timed out")

        with gemini_keyed(hang):
            try:
                enrich.call_gemini("p")
                raise AssertionError("expected ProviderPaused")
            except enrich.ProviderPaused:
                pass
        assert calls == ["g-flash", "g-flash-2"], calls


def test_geminis_day_is_the_pacific_one():
    # January: UTC-8, the quota day turns at 08:00 UTC. July: UTC-7, at 07:00 UTC.
    for moment, day in ((WINTER.replace(hour=7, minute=59), "2026-01-14"), (WINTER.replace(hour=8), "2026-01-15"),
                        (SUMMER.replace(hour=6, minute=59), "2026-07-14"), (SUMMER.replace(hour=7), "2026-07-15")):
        with Patch((db, "utcnow", lambda: moment)):
            assert enrich._today("gemini") == day, (moment, enrich._today("gemini"))
            assert enrich._today("groq") == moment.date().isoformat()
    # The US switch days: second Sunday of March, first Sunday of November (2026: 8 Mar, 1 Nov).
    assert enrich._pacific_offset_hours(datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc)) == -8
    assert enrich._pacific_offset_hours(datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc)) == -7
    assert enrich._pacific_offset_hours(datetime(2026, 11, 1, 8, 59, tzinfo=timezone.utc)) == -7
    assert enrich._pacific_offset_hours(datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)) == -8


def test_an_early_morning_mark_does_not_hold_gemini_off_after_its_quota_returns():
    """26 Nov 2026: at 00:06 UTC Gemini was still on the previous Pacific day's spent quota, the run
    marked it exhausted, and the mark held it off for the rest of the UTC day."""
    with fresh_db() as eng:
        with Patch((db, "utcnow", lambda: WINTER.replace(hour=0, minute=6))):
            enrich.record_usage(eng, "gemini", 0, exhausted=True)
            with eng.connect() as conn:
                assert enrich.allowance(conn, "gemini") == 0
        with Patch((db, "utcnow", lambda: WINTER.replace(hour=8, minute=30))):
            with eng.connect() as conn:
                assert enrich.allowance(conn, "gemini") > 0
                # Groq's day is UTC: a mark at 00:06 holds all day.
            enrich.record_usage(eng, "groq", 0, exhausted=True)
        with Patch((db, "utcnow", lambda: WINTER.replace(hour=23, minute=30))):
            with eng.connect() as conn:
                assert enrich.allowance(conn, "groq") == 0


def test_the_daily_budget_counts_every_gemini_model():
    import os

    if not os.environ.get("GEMINI_DAILY_BUDGET"):
        assert config.DAILY_BUDGET["gemini"] == 18 * (1 + len(config.GEMINI_FALLBACK_MODELS)), config.DAILY_BUDGET
    if not os.environ.get("GEMINI_FALLBACK_MODELS"):
        assert any("lite" in m for m in config.GEMINI_FALLBACK_MODELS), config.GEMINI_FALLBACK_MODELS


# ---------------------------------------------------------------------------- Groq

def test_groq_waits_for_the_limit_it_hit_not_the_days_refill():
    # 26 Nov 2026: a minute's-tokens 429 with the day's request bucket one request short. The old
    # rule waited max(reset-tokens, reset-requests) = 86 s; retry-after says 7.
    headers = {"retry-after": "7", "x-ratelimit-remaining-requests": "999", "x-ratelimit-reset-requests": "1m26.4s",
               "x-ratelimit-reset-tokens": "6.2s"}
    assert enrich.groq_429_wait(headers, "Rate limit reached on tokens per minute (TPM)") == (False, 7.0)
    # Tokens per day: retry-after is the wait (here 4 minutes), and without it the message says.
    assert enrich.groq_429_wait({"retry-after": "248"}, "tokens per day (TPD)") == (False, 248.0)
    msg = "on tokens per day (TPD): Limit 200000, Used 196943, Requested 3630. Please try again in 4m7.536s."
    assert abs(enrich.groq_429_wait({}, msg)[1] - 247.536) < 0.01
    # Out of requests for the day: the model is done.
    assert enrich.groq_429_wait({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "7m12s"}, "")[0]


def test_groq_paces_the_next_call_by_the_tokens_it_lacks():
    # Measured 26 Nov: 4,543 of 8,000 left after a 3,886-token call: the next one fits now.
    full = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "4543", "x-ratelimit-reset-tokens": "25.9s"}
    assert enrich.groq_pace(full, 3886) == 0.0
    low = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "700", "x-ratelimit-reset-tokens": "54.75s"}
    wait = enrich.groq_pace(low, 3000)
    assert 17.0 < wait < 18.5, wait  # 2,300 of the 7,300 missing tokens: under a third of the full 55 s
    assert enrich.groq_pace({}, 3000) == 0.0


def test_groq_asks_gpt_oss_to_reason_briefly_and_qwen_as_before():
    bodies = []

    def post(url, headers=None, json=None, timeout=None):  # noqa: A002
        import json as _json
        bodies.append(json)
        if json["model"] != "openai/gpt-oss-120b":
            return Resp(429, {"error": {"message": "tokens per minute (TPM)"}}, {"retry-after": "200"})
        return Resp(200, {"choices": [{"message": {"content": _json.dumps(ANSWER)}}], "usage": {"total_tokens": 3000}},
                    {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "5000", "x-ratelimit-reset-tokens": "22s"})

    saved = dict(enrich._groq_wait_until), set(enrich._groq_dead)
    enrich._groq_wait_until.clear()
    enrich._groq_dead.clear()
    try:
        with Patch((config, "GROQ_API_KEY", "k"), (config, "GROQ_MODEL", "qwen/qwen3.8-27b"),
                   (config, "GROQ_FALLBACK_MODELS", ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]),
                   (enrich.requests, "post", post)):
            assert enrich.call_groq("p")["headline"] == "Acme ships a model"
            assert [b["model"] for b in bodies] == ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
            assert "reasoning_effort" not in bodies[0] and bodies[1]["reasoning_effort"] == "low"
            # qwen waits the 200 s retry-after (over the 20 s the run will sleep): the next call skips it.
            assert enrich._groq_wait_until["qwen/qwen3.8-27b"] - time.time() > 150
            assert "openai/gpt-oss-120b" not in enrich._groq_wait_until  # 5,000 left covers the next call
    finally:
        enrich._groq_wait_until.clear()
        enrich._groq_wait_until.update(saved[0])
        enrich._groq_dead.clear()
        enrich._groq_dead.update(saved[1])


# ---------------------------------------------------------------------------- Mistral

def test_mistral_without_a_plan_is_asked_once_a_day_not_once_a_run():
    with fresh_db():
        calls = []

        def post(url, headers=None, json=None, timeout=None):  # noqa: A002
            calls.append(url)
            return Resp(402, {"object": "error", "message": "You need a subscription to access this API.",
                              "type": "billing_no_api_budget_configured"})

        with Patch((config, "MISTRAL_API_KEY", "test-key"), (enrich.requests, "post", post)):
            try:
                enrich.call_mistral("p")
                raise AssertionError("expected ProviderPaused")
            except enrich.ProviderPaused:
                pass
            enrich.mistral_reset()  # the next run, a new process: the pause is read back from llm_usage
            assert "not available on this plan" in (enrich.mistral_block() or ""), enrich.mistral_block()
            assert len(calls) == 1


# ---------------------------------------------------------------------------- the two workers

def seed(eng, n):
    with eng.begin() as conn:
        conn.execute(insert(db.sources).values(id=1, key="press", name="Press", url="https://press.test/feed",
                                               source_type="press", weight=1.0))
        now = db.utcnow()
        for i in range(1, n + 1):
            # Article 1 has waited longest, so it is the front of the queue (the scores are otherwise equal).
            conn.execute(insert(db.articles).values(
                id=i, url=f"https://press.test/{i}", source_id=1, title=f"Acme ships a model {i}", domain="press.test",
                published_at=now - timedelta(minutes=n + 1 - i), created_at=now - timedelta(minutes=n + 1 - i), fetched_at=now,
                status="gated", content_text="Acme released a model on Monday. " * 60, description="d"))


def models_by_id(eng):
    with eng.connect() as conn:
        return dict(conn.execute(select(db.articles.c.id, db.articles.c.enrich_model)).all())


def run_with(api_fn, local_fn, budget=60, local_share=10):
    return Patch((config, "GROQ_API_KEY", "k"), (config, "CHECK_SUMMARIES", False),
                 (config, "ENRICH_TIME_BUDGET_SECONDS", budget), (config, "ENRICH_FIRST_ARTICLE_ESTIMATE_SECONDS", 1),
                 (config, "MAX_ENRICH_LOCAL_PER_RUN", local_share),
                 (enrich, "allowance", lambda conn, p: 20), (enrich, "ollama_available", lambda: True),
                 (enrich, "call_groq", api_fn), (enrich, "call_ollama", local_fn),
                 (enrich, "front_page_words", lambda conn: set()))


def test_the_local_model_works_beside_the_api_and_takes_the_back_of_the_queue():
    """The API is rate limited on its first try, then answers in a moment; the local model takes
    one article from the back while the API side does the rest."""
    with fresh_db() as eng:
        seed(eng, 5)
        state = {"api": 0}
        local_started = threading.Event()

        def api(prompt):
            state["api"] += 1
            if state["api"] == 1:
                raise RuntimeError("groq qwen rate limited for 1s")
            local_started.wait(2)  # the local model is already at work: the two overlap
            return dict(ANSWER)

        def local(prompt):
            local_started.set()
            time.sleep(0.3)
            return dict(ANSWER)

        with run_with(api, local):
            stats = enrich.run()
        models = models_by_id(eng)
        assert stats["enriched"] == 5, stats
        local_ids = [i for i, m in models.items() if m.startswith("ollama")]
        assert local_ids == [5], models  # the back of the queue: what the API side would reach last
        assert stats["by_provider"] == {"groq": 4, "ollama": 1}, stats


def test_with_the_api_out_for_the_run_the_local_model_takes_the_queue_in_order():
    with fresh_db() as eng:
        seed(eng, 3)
        order = []

        def api(prompt):
            raise enrich.ProviderPaused("groq out for this run")

        def local(prompt):
            order.append(prompt.split("Article title: ")[1].split("\n")[0])
            return dict(ANSWER)

        with run_with(api, local):
            stats = enrich.run()
        assert stats["enriched"] == 3, stats
        assert order == ["Acme ships a model 1", "Acme ships a model 2", "Acme ships a model 3"], order


def test_the_local_share_and_the_time_budget_still_hold():
    with fresh_db() as eng:
        seed(eng, 6)

        def api(prompt):
            raise enrich.QuotaExhausted("groq out for the day")

        def local(prompt):
            return dict(ANSWER)

        with run_with(api, local, local_share=2):
            stats = enrich.run()
        assert stats["enriched"] == 2, stats  # the local model's share for the run
    with fresh_db() as eng:
        seed(eng, 6)
        t0 = time.monotonic()

        def slow_local(prompt):
            time.sleep(0.6)
            return dict(ANSWER)

        with run_with(api, slow_local, budget=1):
            stats = enrich.run()
        assert stats.get("stopped_for_time"), stats
        assert stats["enriched"] <= 2, stats
        assert time.monotonic() - t0 < 5.0


def test_a_failed_local_answer_leaves_the_article_waiting():
    with fresh_db() as eng:
        seed(eng, 2)

        def api(prompt):
            raise enrich.ProviderPaused("groq out for this run")

        def local(prompt):
            raise ValueError("Expecting ',' delimiter: line 89 column 4")

        with run_with(api, local):
            stats = enrich.run()
        assert stats["enriched"] == 0 and stats["errors"] == 2, stats
        with eng.connect() as conn:
            assert {r.status for r in conn.execute(select(db.articles.c.status)).all()} == {"gated"}


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
