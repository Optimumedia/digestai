"""Cloudflare Workers AI as a provider: first choice for upgrade.py, a fallback after Groq elsewhere,
held by its free daily neurons.

Offline, with a fake HTTP session, on a temporary SQLite database (or on Postgres with
TEST_POSTGRES_URL): python tests/test_cloudflare.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import insert, select  # noqa: E402
from test_mistral import ANSWER, NOW, Patch, Resp, fresh_db  # noqa: E402  (also blanks real keys)

from digest import admin, config, db, enrich, upgrade  # noqa: E402

NEMOTRON = "@cf/nvidia/nemotron-3-120b-a12b"
QWEN = "@cf/qwen/qwen3.8-27b"
LLAMA = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
URL = "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1/chat/completions"


class FakeCloudflare:
    """Stands in for requests.post: per-model status codes, answers with usage.neurons."""

    def __init__(self, status: dict | None = None, neurons=213.6, content=ANSWER):
        self.status, self.neurons, self.content = status or {}, neurons, content
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        assert url == URL, url
        assert headers["Authorization"] == "Bearer tok"
        self.calls.append(json)
        code = self.status.get(json["model"], 200)
        if code == 403:
            return Resp(403, {"errors": [{"message": "not available on the Workers Free plan"}]})
        if code != 200:
            return Resp(code, {"errors": [{"message": "limit"}]})
        return Resp(200, {"choices": [{"message": {"content": self.content}}],
                          "usage": {"prompt_tokens": 3572, "completion_tokens": 389, "neurons": self.neurons}})


def keyed(*extra):
    return Patch((config, "CLOUDFLARE_ACCOUNT_ID", "acct"), (config, "CLOUDFLARE_AI_TOKEN", "tok"),
                 (config, "CLOUDFLARE_AI_MODEL", NEMOTRON), (config, "CLOUDFLARE_AI_FALLBACK_MODELS", [QWEN, LLAMA]),
                 (config, "CLOUDFLARE_DAILY_NEURONS", 9000.0), (config, "CLOUDFLARE_FALLBACK_SHARE", 0.4),
                 (config, "CLOUDFLARE_MAX_PER_RUN", 20), (db, "utcnow", lambda: NOW), *extra)


def neurons_row(eng, day: str, n: int, provider: str = enrich.CF_NEURONS):
    with eng.begin() as conn:
        conn.execute(insert(db.llm_usage).values(day=day, provider=provider, requests=n, exhausted=False))


def test_upgrade_puts_cloudflare_first_and_mistral_last():
    with fresh_db():
        with keyed((config, "GEMINI_API_KEY", "k"), (config, "OLLAMA_API_KEY", "k"), (config, "MISTRAL_API_KEY", "k"),
                   (config, "STRONG_PROVIDERS", ["cloudflare", "gemini", "cloud"]),
                   (enrich, "spare", lambda conn, p: 5), (enrich, "allowance", lambda conn, p: 5)):
            with db.engine().connect() as conn:
                names = [n.split(":")[0] for n, _fn in upgrade._providers(conn)]
                fns = [fn for _n, fn in upgrade._providers(conn)]
            assert names == ["cloudflare", "gemini", "cloud", "mistral"], names
            assert fns[0] is enrich.call_cloudflare_upgrade  # the whole day's neurons, not the fallback share


def test_neurons_limit_per_purpose():
    with fresh_db() as eng:
        # 3,500 neurons used today: the fallback share (40% of 9,000 = 3,600) cannot fit another
        # call, and upgrade.py, which has the whole day, still can.
        neurons_row(eng, NOW.date().isoformat(), 3500)
        fake = FakeCloudflare()
        with keyed((enrich.requests, "post", fake)):
            assert "daily neurons reached" in enrich.cloudflare_block(purpose="fallback", prompt="p")
            assert enrich.cloudflare_allowance() == 0
            try:
                enrich.call_cloudflare("p")
                raise AssertionError("the fallback share must hold")
            except enrich.ProviderPaused:
                pass
            assert fake.calls == []
            assert enrich.call_cloudflare_upgrade("p")["headline"] == "Acme ships a model"
            assert len(fake.calls) == 1
    with fresh_db() as eng:
        neurons_row(eng, NOW.date().isoformat(), 8800)  # a call's worst case (~270) would pass 9,000
        fake = FakeCloudflare()
        with keyed((enrich.requests, "post", fake)):
            try:
                enrich.call_cloudflare_upgrade("p")
                raise AssertionError("the daily limit must hold for upgrades too")
            except enrich.ProviderPaused as exc:
                assert "daily neurons reached" in str(exc)
            assert fake.calls == []


def test_the_day_rolls_over_at_midnight_utc():
    with fresh_db() as eng:
        neurons_row(eng, "2026-09-20", 9000)
        clock = {"now": datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)}
        with keyed((db, "utcnow", lambda: clock["now"]), (enrich.requests, "post", FakeCloudflare())):
            assert enrich.cloudflare_block(purpose="upgrade")
            clock["now"] = datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc)
            assert enrich.cloudflare_block(purpose="upgrade") is None
            enrich.call_cloudflare_upgrade("p")
            with eng.connect() as conn:
                u = enrich.cloudflare_usage(conn)
            assert u["calls"] == 1 and u["neurons"] == 214, u


def test_403_skips_the_model_for_24_hours():
    with fresh_db():
        clock = {"now": NOW}
        fake = FakeCloudflare(status={NEMOTRON: 403})
        with keyed((db, "utcnow", lambda: clock["now"]), (enrich.requests, "post", fake)):
            assert enrich.call_cloudflare("p")["headline"] == "Acme ships a model"
            assert [c["model"] for c in fake.calls] == [NEMOTRON, QWEN]
            # A new run (a new process) does not ask Nemotron again for 24 hours.
            for hours in (1, 23.5):
                enrich.cloudflare_reset()
                clock["now"] = NOW + timedelta(hours=hours)
                enrich.call_cloudflare("p")
                assert fake.calls[-1]["model"] == QWEN, hours
            enrich.cloudflare_reset()
            clock["now"] = NOW + timedelta(hours=24, minutes=5)
            fake.status = {}
            enrich.call_cloudflare("p")
            assert fake.calls[-1]["model"] == NEMOTRON
        # Every model refusing: Cloudflare sits out, it is not an error of the article.
        with keyed((enrich.requests, "post", FakeCloudflare(status={NEMOTRON: 403, QWEN: 403, LLAMA: 403}))):
            enrich.cloudflare_reset()
            try:
                enrich.call_cloudflare("p")
                raise AssertionError("no model left must pause Cloudflare")
            except enrich.ProviderPaused:
                pass


def test_429_pauses_for_the_run():
    with fresh_db():
        fake = FakeCloudflare(status={NEMOTRON: 429})
        with keyed((enrich.requests, "post", fake)):
            try:
                enrich.call_cloudflare("p")
                raise AssertionError("a 429 must pause Cloudflare")
            except enrich.ProviderPaused:
                pass
            try:
                enrich.call_cloudflare("p")
            except enrich.ProviderPaused as exc:
                assert "paused for this run" in str(exc)
            assert len(fake.calls) == 1
            enrich.cloudflare_reset()
            assert enrich.cloudflare_block() is None


def test_usage_recorded_and_the_request_turns_thinking_off():
    with fresh_db() as eng:
        harmony = "<|start|>assistant<|channel|>final<|message|>" + ANSWER + "<|return|>"
        fake = FakeCloudflare(neurons=213.6, content=harmony)
        with keyed((enrich.requests, "post", fake)):
            assert enrich.call_cloudflare("p")["headline"] == "Acme ships a model"  # harmony tags stripped
            enrich.call_cloudflare_upgrade("p")
            enrich.record_usage(eng, "cloudflare", 1)  # a caller's own count is dropped: no double count
            with eng.connect() as conn:
                rows = {(r.day, r.provider): r.requests for r in conn.execute(select(db.llm_usage)).all()}
                card = admin.cloudflare_card(conn)
        today = NOW.date().isoformat()
        assert rows[(today, "cloudflare")] == 2 and rows[(today, enrich.CF_NEURONS)] == 428, rows
        assert fake.calls[0]["chat_template_kwargs"] == {"enable_thinking": False}
        assert fake.calls[0]["response_format"] == {"type": "json_object"}
        assert card["line"] == "Cloudflare: 2 calls today · 428 of 9,000 neurons", card
        assert card["callsToday"] == 2 and card["limitNeurons"] == 9000.0


def test_run_cap():
    with fresh_db():
        fake = FakeCloudflare()
        with keyed((enrich.requests, "post", fake), (config, "CLOUDFLARE_MAX_PER_RUN", 2)):
            enrich.call_cloudflare("p")
            enrich.call_cloudflare("p")
            try:
                enrich.call_cloudflare("p")
                raise AssertionError("the run's call cap must hold")
            except enrich.ProviderPaused:
                pass
            assert len(fake.calls) == 2


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
