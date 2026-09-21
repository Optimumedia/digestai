"""OpenRouter's free models as a provider: first for the day's top rewrites (upgrade.py), a fallback
after Cloudflare elsewhere, held by a daily request count that includes failures.

Offline, with a fake HTTP session, on a temporary SQLite database (or on Postgres with
TEST_POSTGRES_URL): python tests/test_openrouter.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import insert, select, update  # noqa: E402
from test_mistral import ANSWER, NOW, Patch, Resp, fresh_db  # noqa: E402  (also blanks real keys)

from digest import admin, config, db, enrich, upgrade  # noqa: E402

ULTRA = "nvidia/nemotron-3-ultra-550b-a55b:free"
SUPER = "nvidia/nemotron-3-super-120b-a12b:free"
GLM = "z-ai/glm-5.2:free"
URL = "https://openrouter.ai/api/v1/chat/completions"


class FakeOpenRouter:
    """Stands in for requests.post. `status` maps a model to an HTTP code, or to ("body", code) for
    an error that comes back with HTTP 200 and an error object, as an overloaded upstream does."""

    def __init__(self, status: dict | None = None, content=ANSWER):
        self.status, self.content = status or {}, content
        self.calls: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        assert url == URL, url
        assert headers["Authorization"] == "Bearer k"
        self.calls.append(json)
        self.headers.append(headers)
        code = self.status.get(json["model"], 200)
        if isinstance(code, tuple):
            return Resp(200, {"error": {"code": code[1], "message": "provider_overloaded"}})
        if code != 200:
            return Resp(code, {"error": {"code": code, "message": "limit"}})
        return Resp(200, {"choices": [{"message": {"content": self.content}}],
                          "usage": {"prompt_tokens": 3572, "completion_tokens": 484, "cost": 0}})


def keyed(*extra):
    return Patch((config, "OPENROUTER_API_KEY", "k"), (config, "OPENROUTER_URL", "https://openrouter.ai/api/v1"),
                 (config, "OPENROUTER_MODELS", [ULTRA, SUPER, GLM]), (config, "OPENROUTER_DAILY_REQUESTS", 45),
                 (config, "OPENROUTER_FALLBACK_REQUESTS", 30), (config, "OPENROUTER_UPGRADE_DAILY", 5),
                 (config, "OPENROUTER_UPGRADE_MIN_IMPORTANCE", 8), (db, "utcnow", lambda: NOW), *extra)


def calls_row(eng, day: str, n: int, provider: str = enrich.OR_CALLS):
    with eng.begin() as conn:
        conn.execute(insert(db.llm_usage).values(day=day, provider=provider, requests=n, exhausted=False))


def usage_rows(eng) -> dict:
    with eng.connect() as conn:
        return {(r.day, r.provider): r.requests for r in conn.execute(select(db.llm_usage)).all()}


def test_only_free_model_ids_are_ever_sent():
    with fresh_db():
        fake = FakeOpenRouter()
        with keyed((enrich.requests, "post", fake), (config, "OPENROUTER_MODELS", ["openai/gpt-5.5", "nvidia/nemotron-3-ultra-550b-a55b", SUPER])):
            assert enrich.openrouter_models() == [SUPER]
            enrich.call_openrouter("p")
            assert [c["model"] for c in fake.calls] == [SUPER]
        with keyed((enrich.requests, "post", fake), (config, "OPENROUTER_MODELS", ["openai/gpt-5.5"])):
            enrich.openrouter_reset()
            assert "no free model" in (enrich.openrouter_block() or "")
            try:
                enrich.call_openrouter("p")
                raise AssertionError("a paid model id must never be called")
            except enrich.ProviderPaused:
                pass
        assert all(c["model"].endswith(":free") for c in fake.calls)


def test_429_and_503_go_to_the_next_model_and_are_not_retried_this_run():
    with fresh_db() as eng:
        fake = FakeOpenRouter(status={ULTRA: 429, SUPER: 503})
        with keyed((enrich.requests, "post", fake)):
            assert enrich.call_openrouter("p")["headline"] == "Acme ships a model"
            assert [c["model"] for c in fake.calls] == [ULTRA, SUPER, GLM]
            enrich.call_openrouter("p")
            assert [c["model"] for c in fake.calls][3:] == [GLM], "the failed models are not asked again this run"
            # Every request counts, failures included: OpenRouter counts them too.
            assert usage_rows(eng)[(NOW.date().isoformat(), "openrouter")] == 4
            # The request carries the site's name and never lets the model think at length.
            assert fake.headers[0]["HTTP-Referer"] == "https://digestai.news" and fake.headers[0]["X-Title"] == "Digest AI"
            assert fake.calls[0]["reasoning"] == {"enabled": False}
            # A new run asks the first model again.
            enrich.openrouter_reset()
            fake.status = {}
            enrich.call_openrouter("p")
            assert fake.calls[-1]["model"] == ULTRA


def test_an_error_returned_with_http_200_is_handled():
    with fresh_db():
        fake = FakeOpenRouter(status={ULTRA: ("body", 503)})
        with keyed((enrich.requests, "post", fake)):
            assert enrich.call_openrouter("p")["headline"] == "Acme ships a model"
            assert [c["model"] for c in fake.calls] == [ULTRA, SUPER]
        # Every model answering 200 with an error: OpenRouter sits out, the article is not lost.
        fake = FakeOpenRouter(status={ULTRA: ("body", 503), SUPER: ("body", 429), GLM: ("body", 502)})
        with keyed((enrich.requests, "post", fake)):
            enrich.openrouter_reset()
            try:
                enrich.call_openrouter("p")
                raise AssertionError("errors inside a 200 must not pass as answers")
            except enrich.ProviderPaused:
                pass
            assert len(fake.calls) == 3


def test_402_skips_openrouter_for_24_hours():
    with fresh_db():
        clock = {"now": NOW}
        fake = FakeOpenRouter(status={ULTRA: 402})
        with keyed((db, "utcnow", lambda: clock["now"]), (enrich.requests, "post", fake)):
            try:
                enrich.call_openrouter("p")
                raise AssertionError("a 402 must pause OpenRouter")
            except enrich.ProviderPaused:
                pass
            assert [c["model"] for c in fake.calls] == [ULTRA], "never falls through after a 402"
            for hours in (1, 23.9):
                enrich.openrouter_reset()
                clock["now"] = NOW + timedelta(hours=hours)
                assert "payment required" in (enrich.openrouter_block() or ""), hours
            enrich.openrouter_reset()
            clock["now"] = NOW + timedelta(hours=24, minutes=1)
            assert enrich.openrouter_block() is None


def test_daily_requests_per_purpose_and_the_rollover():
    with fresh_db() as eng:
        calls_row(eng, NOW.date().isoformat(), 30)
        fake = FakeOpenRouter()
        with keyed((enrich.requests, "post", fake)):
            # 30 sent today: the fallback's share is used, the top rewrites still have 15.
            assert "daily requests reached" in enrich.openrouter_block(purpose="fallback")
            assert enrich.openrouter_allowance() == 0
            try:
                enrich.call_openrouter("p")
                raise AssertionError("the fallback share must hold")
            except enrich.ProviderPaused:
                pass
            assert fake.calls == []
            assert enrich.openrouter_top_left() == 5
            enrich.call_openrouter_upgrade("p")
            assert len(fake.calls) == 1
    with fresh_db() as eng:
        calls_row(eng, NOW.date().isoformat(), 45)
        fake = FakeOpenRouter()
        with keyed((enrich.requests, "post", fake)):
            assert enrich.openrouter_top_left() == 0
            try:
                enrich.call_openrouter_upgrade("p")
                raise AssertionError("45 a day holds for the top rewrites too")
            except enrich.ProviderPaused:
                pass
            assert fake.calls == []
    with fresh_db() as eng:
        calls_row(eng, "2026-09-20", 45)
        clock = {"now": datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)}
        with keyed((db, "utcnow", lambda: clock["now"]), (enrich.requests, "post", FakeOpenRouter())):
            assert enrich.openrouter_block(purpose="upgrade")
            clock["now"] = datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc)
            assert enrich.openrouter_block(purpose="upgrade") is None


def test_admin_line():
    with fresh_db() as eng:
        calls_row(eng, NOW.date().isoformat(), 7)
        with keyed():
            with eng.connect() as conn:
                card = admin.openrouter_card(conn)
        assert card["line"] == "OpenRouter: 7 of 45 free calls today", card
        assert card["model"] == ULTRA and card["topLimit"] == 5


def test_upgrade_gives_openrouter_the_top_stories_and_cloudflare_the_rest():
    import test_summaries as ts  # seed_story and the multi-source answer

    class Router:
        def __init__(self):
            self.urls: list[str] = []

        def __call__(self, url, headers=None, json=None, timeout=None):  # noqa: A002
            self.urls.append(url)
            body = {"choices": [{"message": {"content": json_dumps(ts.MULTI_ANSWER)}}], "usage": {"neurons": 400}}
            return Resp(200, body)

    def json_dumps(x):
        return json.dumps(x)

    patches = ((config, "GEMINI_API_KEY", ""), (config, "OLLAMA_API_KEY", ""), (config, "MISTRAL_API_KEY", ""),
               (config, "CLOUDFLARE_ACCOUNT_ID", "acct"), (config, "CLOUDFLARE_AI_TOKEN", "tok"),
               (config, "STRONG_PROVIDERS", ["openrouter", "cloudflare", "gemini", "cloud"]),
               (upgrade.time, "sleep", lambda s: None))
    for importance, expected in ((8, "openrouter:"), (6, "cloudflare:")):
        with fresh_db() as eng:
            ts.seed_story(eng)
            with eng.begin() as conn:
                conn.execute(update(db.stories).values(importance=importance))
            router = Router()
            with keyed((enrich.requests, "post", router), (db, "utcnow", lambda: datetime.now(timezone.utc)), *patches):
                stats = upgrade.run()
            assert stats["upgraded"] == 1, stats
            with eng.connect() as conn:
                lead = conn.execute(select(db.articles.c.enrich_model).where(db.articles.c.id == 1)).scalar()
            assert lead.startswith(expected), (importance, lead)
            top = usage_rows(eng).get((datetime.now(timezone.utc).date().isoformat(), "openrouter_top"), 0)
            assert top == (1 if expected == "openrouter:" else 0), top


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
