# Digest AI (digestai.news) v2

An AI news desk that runs on free tiers: a scheduled Python pipeline gathers, extracts,
enriches, clusters and ranks AI news, and a static Astro site publishes it to Cloudflare Pages.

```
pipeline/   Python ingestion pipeline (GitHub Actions, hourly)
site/       Astro static site (week 2)
supabase/   SQL to run once in the Supabase project (indexes, row security, editor views)
```

## Pipeline steps

| step    | what it does                                                                   |
|---------|--------------------------------------------------------------------------------|
| fetch   | pulls new links from `pipeline/digest/sources.yaml` (RSS, Hacker News, Reddit)  |
| extract | fetches each page and extracts the article body (JSON-LD, trafilatura, readability) with boilerplate scrubbing and validation |
| gate    | English only, AI relevance score, spam and press-release filters, date sanity, duplicate titles |
| enrich  | one Gemini Flash call per article (Groq fallback): headline, digest, key points, why it matters, category, entities, importance, and a practical AI at Work card when a small team can act on it |
| cluster | local sentence embeddings group articles covering the same event into one story |
| threads | groups stories about the same saga over 14 days into developing threads ("the story so far") |
| pulse   | summarises the top Hacker News comments on well-discussed stories (LLM, budgeted) |
| discuss | finds the Hacker News thread for articles that arrived via feeds (Algolia API) |
| rank    | learns from reader events which articles perform, predicts for new ones, scores stories, adjusts source weights, spins up discovery feeds for hot topics |
| export  | writes `site/src/data/*.json` for the site build, including the daily briefing selection and the AI at Work section (`work.json`, `work-briefing.json`) |
| push    | sends one breaking story per run (three a day at most) to browsers that turned on alerts, via Web Push with our own VAPID keys; prunes dead subscriptions |
| topics  | writes model-authored introductions for topic hubs (companies, models, people) |
| intros  | writes introductions for weekly recaps, the model and funding trackers and the AI at Work tool directory |
| images  | renders a 1200×630 share card per story into `site/public/og/` |
| audio   | reads the briefing aloud once a day (Kokoro neural voice on the runner, Piper as fallback, MP3) and publishes /podcast.xml and /listen; synthesis is resumable, so a long episode is finished by the next run |
| newsletter | builds the daily briefing email and sends it through Kit once a day at `NEWSLETTER_HOUR_UTC` |

## Local run

```bash
python -m venv .venv
.venv/Scripts/pip install -r pipeline/requirements.txt      # Windows
cd pipeline && ../.venv/Scripts/python -m digest.run all
```

Without any keys it runs end to end against a local SQLite file (`pipeline/data/digest.db`)
using heuristic summaries. Add a `.env` at the repo root to use the real services:

```
DATABASE_URL=postgresql://...        # Supabase > Project Settings > Database > connection string (session mode)
GEMINI_API_KEY=...                   # https://aistudio.google.com/apikey (free tier)
GROQ_API_KEY=...                     # https://console.groq.com/keys (free tier, fallback)
```

Test extraction on one URL:

```bash
cd pipeline && ../.venv/Scripts/python tests/run_harness.py --url https://example.com/article
```

## Site

```bash
cd site && npm install
npm run build        # reads site/src/data/*.json written by the pipeline's export step
npm run preview      # http://localhost:4321
```

Pages: front page, `/today`, `/story/<slug>` (digest, key points, why it matters, community pulse,
the story so far, credited full text, coverage and discussion), `/thread/<slug>` and `/threads`
(developing stories with timelines), `/models` and `/funding` (trackers extracted from the news),
`/week/<iso-week>` (weekly recap), `/river` (five days of headlines), `/category/<key>`,
`/topic/<entity>`, `/daily/<date>`, `/saved`, `/search` (Pagefind, static), `/rss.xml`,
`/work` (AI at Work: practical AI for marketing, customers and small-business admin, with its own
briefing, sub-menu and accent), `/work/tools` (tool directory), `/work/week/<iso-week>` (playbook),
`/work/rss.xml`,
`/news-sitemap.xml` (Google News, last 48 h), `/sitemap-index.xml`, `/about`, `/sources`.

LLM usage is paced: a daily budget per provider (`GEMINI_DAILY_BUDGET`, default 1,200 of the
1,500 free requests) is spread over the day's runs; unused allowance rolls forward within the
day, and a quota error stops calls until the next day.

Reader events (view, dwell, click to source, share, save, follow) are posted to the Supabase `events` table when
`PUBLIC_SUPABASE_URL` and `PUBLIC_SUPABASE_ANON_KEY` are set at build time; `rank.py` learns from them.

Retention features, all without accounts (browser storage): `/today` briefing, Follow on topic and
category pages feeding a "Your topics" strip on the front page, "new since your last visit" badges,
Save to `/saved`, share buttons, live timestamps, Hacker News and Reddit discussion links, coverage bar
by source type with a "primary source" badge, Giscus comments (when `PUBLIC_GISCUS_*` are set),
per-story share images.

## Production setup (once)

1. Create a free Supabase project. Put its Postgres connection string in the `DATABASE_URL`
   repository secret. Run the pipeline once, then run `supabase/schema.sql` in the SQL editor.
2. Create a Gemini API key (AI Studio) and a Groq key. Add both as repository secrets.
3. Make the repository public so GitHub Actions minutes are unlimited, and enable the
   `Ingest and publish` workflow.
4. Add Cloudflare Pages deploy secrets (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`).
5. Newsletter: create a Kit account, a form, and a v4 API key. Secret `KIT_API_KEY`; repository
   variables `PUBLIC_KIT_FORM_URL` and optionally `NEWSLETTER_HOUR_UTC`.
6. Comments: enable Discussions on the repo, install the giscus app, and set the four
   `PUBLIC_GISCUS_*` repository variables from https://giscus.app.
7. Site events: repository secrets `PUBLIC_SUPABASE_URL` and `PUBLIC_SUPABASE_ANON_KEY`
   (the anon key is public by design; `supabase/schema.sql` restricts it to inserting events).

## Admin dashboard

`https://digestai.news/admin` (not indexed, not in the sitemap). Built from `admin.json`, which the
pipeline writes on every run: last-run health, the article funnel per day, who wrote the summaries,
model calls against budgets, run durations, category mix, source performance (7-day fetched /
published / discussed on the web, weight, learned performance, errors), top stories by feed
score, recent runs step by step, and reader engagement once Supabase records events.

Actions on the page (run the pipeline now, pin, unpublish) commit through GitHub's API and need a
personal token with `repo` and `workflow` scopes, stored only in that browser. Pin and unpublish
edit `pipeline/digest/moderation.yaml`; the next run applies them.

## How ranking learns

`rank.py` scores every story from two signal families. External popularity: Hacker News points,
Reddit score, Mastodon trending shares, number of outlets covering the story, whether the primary
source is in it, and how fast that popularity arrived. Internal engagement: views, time on page,
clicks to source, saves, follows and shares from the events table. A ridge regression from
[story embedding + those features] predicts how new stories will do; it trains on reader
engagement when there is enough of it and on web popularity until then. Source weights drift
toward sources whose stories perform, and the hottest entities become temporary search feeds.

## Editorial control

Supabase Studio is the admin panel. `live_stories` shows what is on the site in site order.
`update stories set status='unpublished' where id=...` removes a story on the next build;
`pinned=true` keeps it on the front page.
