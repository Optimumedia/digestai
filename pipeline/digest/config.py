"""Configuration from environment (with an optional .env file next to the repo root)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = ROOT / "pipeline"
DATA_DIR = PIPELINE_DIR / "data"
SITE_DATA_DIR = ROOT / "site" / "src" / "data"
# Rows earlier runs already read (cache.py); kept between runs by the Actions cache.
CACHE_DIR = Path(os.environ.get("CACHE_DIR") or DATA_DIR / "cache")


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

# Social posting (Bluesky). The app password comes from Bluesky settings; never a main password.
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
SOCIAL_MAX_PER_DAY = int(os.environ.get("SOCIAL_MAX_PER_DAY") or 6)       # breaking story posts per day
# Evening recap: 18:00-21:00 UTC is the strongest window for news posts on Bluesky (own study, Sept 2026).
SOCIAL_BRIEFING_HOUR_UTC = int(os.environ.get("SOCIAL_BRIEFING_HOUR_UTC") or 19)
SOCIAL_DRY_RUN = os.environ.get("SOCIAL_DRY_RUN") == "1"

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
# Stop summarising after this many seconds and leave the rest for the next run. Rate-limited
# providers once stretched the step past the workflow's 28-minute limit and got runs cancelled.
ENRICH_TIME_BUDGET_SECONDS = int(os.environ.get("ENRICH_TIME_BUDGET_SECONDS") or "600")
RUNS_PER_DAY = int(os.environ.get("RUNS_PER_DAY") or "48")
# Minutes past the hour the workflow's schedule starts a run (the dashboard's "next run due").
RUN_MINUTES = [int(m) for m in (os.environ.get("RUN_MINUTES") or ("0,30" if RUNS_PER_DAY >= 48 else "7")).split(",") if m.strip()]
RUN_INTERVAL_MINUTES = 24 * 60 // max(1, RUNS_PER_DAY)
# Ranking model refit interval (rank.py). A refit rewrites nearly every prediction, which the next
# run reads back, so once a day; new articles are predicted with the latest fit in between.
RANK_TRAIN_HOURS = float(os.environ.get("RANK_TRAIN_HOURS") or "24")
# Supabase free plan: what the admin page measures the database against. The billing cycle starts on
# this day of the month (the project's usage page shows it).
SUPABASE_EGRESS_GB = float(os.environ.get("SUPABASE_EGRESS_GB") or "5")
SUPABASE_DB_MB = float(os.environ.get("SUPABASE_DB_MB") or "500")
SUPABASE_CYCLE_DAY = int(os.environ.get("SUPABASE_CYCLE_DAY") or "11")
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
# Full story pages for 45 days, then a small archive page at the same address (archive.py): at ~60 KB a
# page, 120 days of full pages would pass GitHub Pages' 1 GB limit.
EXPORT_DAYS = int(os.environ.get("EXPORT_DAYS", "45"))

# --- Phase 0 data fixes (appended) ---
# Discovery: Bing News search feeds for hot companies and models. Bounded in total, not only per
# run: unbounded they produced 93% of inserted rows, mostly repeats and unextractable pages.
MAX_DISCOVERED_SOURCES = int(os.environ.get("MAX_DISCOVERED_SOURCES") or "8")
DISCOVERY_MIN_ARTICLES = int(os.environ.get("DISCOVERY_MIN_ARTICLES") or "2")  # a term must lead 2+ top articles
MAX_DISCOVERED_ITEMS = int(os.environ.get("MAX_DISCOVERED_ITEMS") or "10")  # items taken per discovered feed
# Fetch politeness per host in seconds; hosts not listed wait FETCH_HOST_DELAY between requests.
FETCH_HOST_DELAY = float(os.environ.get("FETCH_HOST_DELAY") or "1.0")
FETCH_SLOW_HOSTS = {"reddit.com": 4.0}
FETCH_WORKERS = int(os.environ.get("FETCH_WORKERS") or "8")  # hosts fetched in parallel
EXTRACT_WORKERS = int(os.environ.get("EXTRACT_WORKERS") or "6")  # article pages downloaded in parallel
# A page's own publication date replaces the feed/submission date when it is this much older.
PAGE_DATE_MIN_GAP_DAYS = float(os.environ.get("PAGE_DATE_MIN_GAP_DAYS") or "3")
# Clustering: a story stops absorbing articles at this size (threads link related stories), and
# oversized stories are split back on later runs, at most this many articles detached per run.
CLUSTER_MAX_ARTICLES = int(os.environ.get("CLUSTER_MAX_ARTICLES") or "40")
CLUSTER_REPAIR_MAX_PER_RUN = int(os.environ.get("CLUSTER_REPAIR_MAX_PER_RUN") or "300")
# An article must also be this close to the story's lead article: the merge threshold minus this
# margin (0.82 - 0.03 = 0.79 with the embedding model). Stops the mean drifting to a generic topic.
CLUSTER_LEAD_MARGIN = float(os.environ.get("CLUSTER_LEAD_MARGIN") or "0.03")
# One story per event (merge.py). Two published stories are the same event when their embeddings are
# this alike and they share a named entity (or are much more alike, or carry the same headline), and
# they broke within MERGE_PAIR_DAYS of each other. The smaller pool of pairs just under the bar is
# listed for review on the dashboard. At most MERGE_MAX_PER_RUN stories are merged per run, among
# stories first published in the last MERGE_LOOKBACK_DAYS (threads read the same window's vectors).
MERGE_THRESHOLD_MODEL = float(os.environ.get("MERGE_THRESHOLD_MODEL") or "0.88")
MERGE_THRESHOLD_FALLBACK = float(os.environ.get("MERGE_THRESHOLD_FALLBACK") or "0.72")
MERGE_PAIR_DAYS = int(os.environ.get("MERGE_PAIR_DAYS") or "7")
MERGE_LOOKBACK_DAYS = int(os.environ.get("MERGE_LOOKBACK_DAYS") or "14")
MERGE_MAX_PER_RUN = int(os.environ.get("MERGE_MAX_PER_RUN") or "10")
# Enrichment: time assumed for one article before any has finished in this run.
ENRICH_FIRST_ARTICLE_ESTIMATE_SECONDS = float(os.environ.get("ENRICH_FIRST_ARTICLE_ESTIMATE_SECONDS") or "90")

CATEGORIES: dict[str, str] = {
    "models": "Generative AI & Models",
    "marketing": "Marketing & Small Business",
    "agents": "Agents & Tools",
    "research": "Research",
    "business": "Business & Funding",
    "policy": "Policy & Regulation",
    "hardware": "Hardware & Compute",
    "enterprise": "Enterprise & Industry",
    "robotics": "Robotics & Physical AI",
    "society": "Society & Work",
}

# Focus categories: a score bonus so their stories hold top places, and a reserved place in the
# daily briefing when one is fresh. Marketing & Small Business (practical AI for marketers and small
# businesses) is one of the site's top three focuses (Martin, 17 Sep 2026).
FOCUS_CATEGORIES: dict[str, float] = {"marketing": 0.06}
BRIEFING_FOCUS_CATEGORY = "marketing"

# Risky-claim hold (hold.py). Off: Martin decided on 14 Sep that news is published without waiting for
# approval. Set HOLD_RISKY_CLAIMS=1 to hold single-source allegations for review again.
HOLD_RISKY_CLAIMS = os.environ.get("HOLD_RISKY_CLAIMS", "0") == "1"
