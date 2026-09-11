"""Configuration from environment (with an optional .env file next to the repo root)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = ROOT / "pipeline"
DATA_DIR = PIPELINE_DIR / "data"
SITE_DATA_DIR = ROOT / "site" / "src" / "data"


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# GitHub Actions passes unset secrets as empty strings, so "" must mean "use the default".
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{(DATA_DIR / 'digest.db').as_posix()}"
SITE_URL = (os.environ.get("SITE_URL") or "https://digestai.news").rstrip("/")
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; DigestAIBot/2.0; +https://digestai.news/about)",
)
# A browser-like agent for publishers that serve an empty shell to unknown bots.
BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 DigestAIBot/2.0"
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Verified 2026-09-11: the 2.5 generation is closed to new accounts; these three accept requests.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
GEMINI_FALLBACK_MODELS = [m.strip() for m in (os.environ.get("GEMINI_FALLBACK_MODELS") or "gemini-3.5-flash,gemini-flash-latest").split(",") if m.strip()]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# Verified 2026-09-11 against the Groq model list; each model has its own free-tier quota.
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "qwen/qwen3.8-27b"
GROQ_FALLBACK_MODELS = [m.strip() for m in (os.environ.get("GROQ_FALLBACK_MODELS") or "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",") if m.strip()]

# Kit (formerly ConvertKit) daily broadcast. 05:00 UTC is 07:00 in Central Europe in winter,
# 08:00 in summer; change NEWSLETTER_HOUR_UTC to taste.
KIT_API_KEY = os.environ.get("KIT_API_KEY", "")

# Browser push alerts (Web Push). Keys from scripts/gen_vapid.py; the public one is embedded in the site.
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
PUBLIC_VAPID_KEY = os.environ.get("PUBLIC_VAPID_KEY", "")
PUSH_MAX_PER_DAY = int(os.environ.get("PUSH_MAX_PER_DAY") or 3)

# Spoken briefing (Piper voice on the runner, MP3 via LAME). Generated once a day at/after this hour.
AUDIO_VOICE = os.environ.get("AUDIO_VOICE") or "en_US-lessac-medium"
AUDIO_HOUR_UTC = int(os.environ.get("AUDIO_HOUR_UTC") or os.environ.get("NEWSLETTER_HOUR_UTC") or 5)
NEWSLETTER_HOUR_UTC = int(os.environ.get("NEWSLETTER_HOUR_UTC", "5"))

# Free tiers, measured 2026-09-11: Gemini 3.x Flash models allow only 20 requests per day per
# model on the free tier (quota id GenerateRequestsPerDayPerProjectPerModel-FreeTier), so the
# three-model chain gives ~60 calls a day; they go to the highest-weight sources. Groq's free
# tier is 14,400/day, so a Groq key lifts the ceiling for everything else. The daily budget is
# spread over the day's runs; unused allowance rolls forward within the day.
# ~30 s per Gemini call on long articles: 20 per run keeps the job well inside the 30-minute cadence.
MAX_ENRICH_PER_RUN = int(os.environ.get("MAX_ENRICH_PER_RUN") or "20")
RUNS_PER_DAY = int(os.environ.get("RUNS_PER_DAY") or "48")
DAILY_BUDGET = {
    "gemini": int(os.environ.get("GEMINI_DAILY_BUDGET") or "54"),
    "groq": int(os.environ.get("GROQ_DAILY_BUDGET") or "2400"),  # 3 models x 1,000/day, 80%
}
# Groq's free tier allows ~8,000 tokens per minute per model, so the article text sent to it is
# shorter than Gemini's and calls are paced from the rate-limit headers.
GROQ_INPUT_WORDS = int(os.environ.get("GROQ_INPUT_WORDS") or "2200")

# Local model through Ollama (used when no API key is configured, e.g. on the GitHub runner).
# A 3B model on a 4-core runner takes ~40 s per article, so the per-run cap is lower.
OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or "qwen2.5:3b"
MAX_ENRICH_LOCAL_PER_RUN = int(os.environ.get("MAX_ENRICH_LOCAL_PER_RUN") or "10")
LOCAL_INPUT_WORDS = int(os.environ.get("LOCAL_INPUT_WORDS") or "1200")
MAX_FETCH_PER_SOURCE = int(os.environ.get("MAX_FETCH_PER_SOURCE", "40"))
MAX_EXTRACT_PER_RUN = int(os.environ.get("MAX_EXTRACT_PER_RUN", "120"))
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "10"))
MAX_ARTICLE_AGE_DAYS = int(os.environ.get("MAX_ARTICLE_AGE_DAYS", "7"))
MIN_WORDS = int(os.environ.get("MIN_WORDS", "250"))
MAX_WORDS = int(os.environ.get("MAX_WORDS", "8000"))
LLM_INPUT_WORDS = int(os.environ.get("LLM_INPUT_WORDS", "5000"))
# Cosine similarity thresholds for merging articles into one story.
CLUSTER_THRESHOLD_MODEL = float(os.environ.get("CLUSTER_THRESHOLD_MODEL", "0.82"))
CLUSTER_THRESHOLD_FALLBACK = float(os.environ.get("CLUSTER_THRESHOLD_FALLBACK", "0.60"))
CLUSTER_WINDOW_HOURS = int(os.environ.get("CLUSTER_WINDOW_HOURS", "72"))
EXPORT_DAYS = int(os.environ.get("EXPORT_DAYS", "120"))

CATEGORIES: dict[str, str] = {
    "models": "Generative AI & Models",
    "agents": "Agents & Tools",
    "research": "Research",
    "business": "Business & Funding",
    "policy": "Policy & Regulation",
    "hardware": "Hardware & Compute",
    "enterprise": "Enterprise & Industry",
    "robotics": "Robotics & Physical AI",
    "society": "Society & Work",
}
