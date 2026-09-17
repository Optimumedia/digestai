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

# Spoken briefing (a neural voice on the runner's CPU, MP3 via LAME). Once a day at/after this hour.
# Engine "kokoro" (default) reads far more like a person than Piper, at about real time on a runner
# core; "piper" is the older, faster engine and the automatic fallback when Kokoro's model files are
# missing. AUDIO_VOICE names a voice of the chosen engine (Kokoro: af_heart, am_michael, bf_emma...).
AUDIO_ENGINE = (os.environ.get("AUDIO_ENGINE") or "kokoro").strip().lower()
AUDIO_VOICE = os.environ.get("AUDIO_VOICE") or ("af_heart" if AUDIO_ENGINE == "kokoro" else "en_US-lessac-medium")
AUDIO_PIPER_VOICE = os.environ.get("AUDIO_PIPER_VOICE") or "en_US-lessac-medium"   # the fallback's voice
AUDIO_LANG = os.environ.get("AUDIO_LANG") or "en-us"
AUDIO_SPEED = float(os.environ.get("AUDIO_SPEED") or 1.0)
# Kokoro costs about 1.5 seconds of CPU per second of speech (measured), so a 3.5-minute episode is
# ~5 minutes of work: too much to do in one run beside everything else. The step stops starting new
# parts after this many seconds and finishes the episode on a later run (audio.py, "resumable"); a
# part is one story, ~60 s, so a run spends at most ~4 minutes here and an episode takes two runs.
AUDIO_TIME_BUDGET_SECONDS = int(os.environ.get("AUDIO_TIME_BUDGET_SECONDS") or 180)
AUDIO_HOUR_UTC = int(os.environ.get("AUDIO_HOUR_UTC") or os.environ.get("NEWSLETTER_HOUR_UTC") or 5)
# AI at Work (/work) gets its own episode, once a week rather than once a day: the section covers
# about six practical items a week (measured on the live section, 11-16 Sep 2026: six cards over six
# days, four of them on one day and three days with none), which is a thin daily show and a good
# weekly one. It is published on WORK_AUDIO_WEEKDAY (0 = Monday) from WORK_AUDIO_HOUR_UTC, and reads
# the ISO week that ended the day before, so the episode is never rewritten once it is out.
WORK_AUDIO = (os.environ.get("WORK_AUDIO") or "1") == "1"   # "" is an unset Actions variable, not "off"
WORK_AUDIO_WEEKDAY = int(os.environ.get("WORK_AUDIO_WEEKDAY") or 0)
WORK_AUDIO_HOUR_UTC = int(os.environ.get("WORK_AUDIO_HOUR_UTC") or AUDIO_HOUR_UTC)
# Items read out, and the fewest a week needs before it is worth an episode at all.
WORK_AUDIO_ITEMS = int(os.environ.get("WORK_AUDIO_ITEMS") or 6)
WORK_AUDIO_MIN_ITEMS = int(os.environ.get("WORK_AUDIO_MIN_ITEMS") or 3)
# Both episodes share AUDIO_TIME_BUDGET_SECONDS, and the daily briefing has first call on it: the
# section only starts a part when at least this much of the run's budget is still unspent, so a
# briefing that is still being read is never slowed down and a run never overshoots by more than
# the one part the briefing itself may be in the middle of.
WORK_AUDIO_MIN_BUDGET_SECONDS = int(os.environ.get("WORK_AUDIO_MIN_BUDGET_SECONDS") or 45)
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
    "groq": int(os.environ.get("GROQ_DAILY_BUDGET") or "2400"),
    # Ollama's free allowance is usage-based (hourly and weekly); a 429 pauses it for the rest of the run.
    "cloud": int(os.environ.get("OLLAMA_CLOUD_DAILY_BUDGET") or "400"),  # 3 models x 1,000/day, 80%
}
# Groq's free tier allows ~8,000 tokens per minute per model, so the article text sent to it is
# shorter than Gemini's and calls are paced from the rate-limit headers. The prompt carries worked
# examples now, so the text sent with it is a little shorter to keep the window where it was.
GROQ_INPUT_WORDS = int(os.environ.get("GROQ_INPUT_WORDS") or "2000")
# The prompt plus the article, per provider, must stay under this many tokens (estimated, never
# measured by a tokeniser): Groq's window is 8,000 tokens a minute and the answer takes up to 3,000
# of them, and the local 3B model runs with a 4,096-token context. Tested in test_summaries.py.
PROMPT_TOKEN_BUDGET = {"groq": 4800, "ollama": 3000}

# --- where the best model goes, and what is checked before a summary is stored (enrich.py) ---
# Waiting articles ranked per run on narrow columns (~150 bytes each): 300 covers two days of
# arrivals, and the oldest few are kept in view so a backlog cannot starve.
ENRICH_QUEUE_POOL = int(os.environ.get("ENRICH_QUEUE_POOL") or "300")
ENRICH_QUEUE_OLDEST = int(os.environ.get("ENRICH_QUEUE_OLDEST") or "25")
# Hours of waiting after which an article's age alone carries it to the front of the queue.
ENRICH_QUEUE_AGE_HOURS = float(os.environ.get("ENRICH_QUEUE_AGE_HOURS") or "24")
ENRICH_FRONT_PAGE_STORIES = int(os.environ.get("ENRICH_FRONT_PAGE_STORIES") or "12")
# Characters of article text read per article: more than the longest prompt can carry (5,000 words).
ENRICH_TEXT_CHARS = int(os.environ.get("ENRICH_TEXT_CHARS") or "40000")
# The rules-only check of a summary against its article (checks.py) and the one paid retry it may
# ask for when something does not check out.
CHECK_SUMMARIES = os.environ.get("CHECK_SUMMARIES", "1") == "1"
CHECK_RETRY_MAX_PER_RUN = int(os.environ.get("CHECK_RETRY_MAX_PER_RUN") or "4")

# --- second pass: stories that turned out to matter get a better summary (upgrade.py) ---
UPGRADE_SUMMARIES = os.environ.get("UPGRADE_SUMMARIES", "1") == "1"
# Providers worth upgrading to, best first; a summary already written by one of them is not redone.
STRONG_PROVIDERS = [p.strip() for p in (os.environ.get("STRONG_PROVIDERS") or "gemini,cloud").split(",") if p.strip()]
UPGRADE_MAX_PER_RUN = int(os.environ.get("UPGRADE_MAX_PER_RUN") or "2")
UPGRADE_DAILY_MAX = int(os.environ.get("UPGRADE_DAILY_MAX") or "12")
# A story has to have proved itself: this many independent sources, or this score (the top fifth
# of the front page), or readers engaging with it.
UPGRADE_MIN_SOURCES = int(os.environ.get("UPGRADE_MIN_SOURCES") or "3")
UPGRADE_MIN_SCORE = float(os.environ.get("UPGRADE_MIN_SCORE") or "0.60")
UPGRADE_LOOKBACK_HOURS = float(os.environ.get("UPGRADE_LOOKBACK_HOURS") or "72")
# A story is never re-done for the same material: only this many new independent sources since the
# last upgrade make it worth writing again.
UPGRADE_NEW_SOURCES = int(os.environ.get("UPGRADE_NEW_SOURCES") or "3")
# The multi-source digest reads this many articles (lead first), this many words of each.
UPGRADE_MULTI_ARTICLES = int(os.environ.get("UPGRADE_MULTI_ARTICLES") or "4")
UPGRADE_ARTICLE_WORDS = int(os.environ.get("UPGRADE_ARTICLE_WORDS") or "1100")
# Upgrades stand aside while the queue is this far behind: new stories come first.
UPGRADE_MAX_WAITING = int(os.environ.get("UPGRADE_MAX_WAITING") or "40")
UPGRADE_TIME_BUDGET_SECONDS = int(os.environ.get("UPGRADE_TIME_BUDGET_SECONDS") or "180")

# Local model through Ollama (used when no API key is configured, e.g. on the GitHub runner).
# A 3B model on a 4-core runner takes ~40 s per article, so the per-run cap is lower.
OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "").rstrip("/")
# Ollama Cloud: large models on Ollama's servers with a free allowance (checked 17 Sep 2026: the free
# account can use gpt-oss 120B/20B, Nemotron 3 Ultra/Super/Nano and Gemma 4; others need credits).
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_CLOUD_URL = (os.environ.get("OLLAMA_CLOUD_URL") or "https://ollama.com").rstrip("/")
OLLAMA_CLOUD_MODEL = os.environ.get("OLLAMA_CLOUD_MODEL") or "gpt-oss:120b"
OLLAMA_CLOUD_FALLBACK_MODELS = [m.strip() for m in (os.environ.get("OLLAMA_CLOUD_FALLBACK_MODELS") or "nemotron-3-ultra,gemma4:31b").split(",") if m.strip()]
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or "qwen2.5:3b"
MAX_ENRICH_LOCAL_PER_RUN = int(os.environ.get("MAX_ENRICH_LOCAL_PER_RUN") or "10")
# The local model answers inside a 4,096-token context (num_ctx in call_ollama) and writes up to
# 900 of them, so prompt and article together have to stay near 3,000: the guidance it gets is the
# short one and the article is cut here.
LOCAL_INPUT_WORDS = int(os.environ.get("LOCAL_INPUT_WORDS") or "1000")
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
# Off since 17 Sep: at 0.88 with a shared name ("Anthropic" is in most AI stories) separate events
# were merged - a Samsung investment, a Fujitsu chip launch and a dozen policy stories went into one.
# Only near-identical wording merges now; MERGE_DUPLICATES=1 turns the similarity rule back on.
MERGE_DUPLICATES = os.environ.get("MERGE_DUPLICATES", "0") == "1"
MERGE_THRESHOLD_MODEL = float(os.environ.get("MERGE_THRESHOLD_MODEL") or "0.97")
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
