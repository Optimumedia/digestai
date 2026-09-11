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

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'digest.db').as_posix()}")
SITE_URL = os.environ.get("SITE_URL", "https://digestai.news").rstrip("/")
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Kit (formerly ConvertKit) daily broadcast. 05:00 UTC is 07:00 in Central Europe in winter,
# 08:00 in summer; change NEWSLETTER_HOUR_UTC to taste.
KIT_API_KEY = os.environ.get("KIT_API_KEY", "")
NEWSLETTER_HOUR_UTC = int(os.environ.get("NEWSLETTER_HOUR_UTC", "5"))

# Free tier: Gemini Flash allows 1,500 requests/day and 15/min. 48 runs/day x 30 = 1,440.
MAX_ENRICH_PER_RUN = int(os.environ.get("MAX_ENRICH_PER_RUN", "30"))
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
