# Digest AI (digestai.news) v2

An AI news desk that runs on free tiers: a scheduled Python pipeline gathers, extracts,
enriches, clusters and ranks AI news, and a static Astro site publishes it to Cloudflare Pages.

```
pipeline/   Python ingestion pipeline (GitHub Actions, every 30 min)
site/       Astro static site (week 2)
supabase/   SQL to run once in the Supabase project (indexes, row security, editor views)
```

## Pipeline steps

| step    | what it does                                                                   |
|---------|--------------------------------------------------------------------------------|
| fetch   | pulls new links from `pipeline/digest/sources.yaml` (RSS, Hacker News, Reddit)  |
| extract | fetches each page and extracts the article body (JSON-LD, trafilatura, readability) with boilerplate scrubbing and validation |
| gate    | English only, AI relevance score, spam and press-release filters, date sanity, duplicate titles |
| enrich  | one Gemini Flash call per article (Groq fallback): headline, digest, key points, why it matters, category, entities, importance |
| cluster | local sentence embeddings group articles covering the same event into one story |
| discuss | finds the Hacker News thread for articles that arrived via feeds (Algolia API) |
| rank    | learns from reader events which articles perform, predicts for new ones, scores stories, adjusts source weights, spins up discovery feeds for hot topics |
| export  | writes `site/src/data/*.json` for the site build, including the daily briefing selection |
| images  | renders a 1200×630 share card per story into `site/public/og/` |
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

Pages: front page, `/story/<slug>` (digest, key points, why it matters, credited full text, other coverage),
`/category/<key>`, `/topic/<entity>`, `/daily/<date>`, `/search` (Pagefind, static), `/rss.xml`,
`/news-sitemap.xml` (Google News, last 48 h), `/sitemap-index.xml`, `/about`, `/sources`.

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

## Editorial control

Supabase Studio is the admin panel. `live_stories` shows what is on the site in site order.
`update stories set status='unpublished' where id=...` removes a story on the next build;
`pinned=true` keeps it on the front page.
