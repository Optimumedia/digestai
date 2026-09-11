# Going live: the parts only you can do

Everything below is a one-time setup. Each step ends with a value that goes into the `.env`
file at the repo root (copy `.env.example` first). When the file is filled in, one script pushes
all of it to GitHub; you never paste a key anywhere else.

Time needed: about 40 minutes. Cost: nothing.

## 0. Put the code on GitHub (5 min)

Open a terminal in this folder:

```powershell
git add -A
git commit -m "Digest AI v2: pipeline, site, tier 1 retention features"
gh auth login            # browser login, choose HTTPS
gh repo create digestai --public --source . --push
```

Public is what makes GitHub Actions free without limits. Secrets are never in the code.

## 1. Supabase (10 min)

1. https://supabase.com → New project. Name `digestai`, any region near your readers, note the
   database password.
2. Project Settings → Database → Connection string → URI, **Session** mode. Put it in `.env` as
   `DATABASE_URL` (replace `[YOUR-PASSWORD]`).
3. Project Settings → API. Copy **Project URL** → `PUBLIC_SUPABASE_URL` and the **anon public** key →
   `PUBLIC_SUPABASE_ANON_KEY`. This key is designed to be public; the SQL in step 6 limits it to
   inserting reader events.

## 2. Gemini and Groq keys (3 min)

- https://aistudio.google.com/apikey → Create API key → `GEMINI_API_KEY`.
- https://console.groq.com/keys → Create key → `GROQ_API_KEY`.

## 3. Kit newsletter (10 min)

1. https://kit.com → sign up for the free Newsletter plan.
2. Grow → Landing Pages & Forms → Create → Form → any inline template → name it "Digest AI daily".
   Publish → the form's **action URL** is what the site posts to. It looks like
   `https://app.kit.com/forms/1234567/subscriptions`. Put it in `.env` as `PUBLIC_KIT_FORM_URL`.
3. Settings → Developer → **API Keys (v4)** → Create → `KIT_API_KEY`.
4. Optional: `NEWSLETTER_HOUR_UTC=5` sends at 07:00 Central European winter time; change if you like.

## 4. Giscus comments (5 min)

1. On GitHub: repo → Settings → General → Features → tick **Discussions**.
2. Discussions tab → Categories (pencil icon) → New category → name **Stories**, format
   **Announcements** (so only the site creates threads).
3. https://github.com/apps/giscus → Install → choose the `digestai` repo.

That is all; the script fetches the two IDs itself.

## 5. Cloudflare Pages (7 min)

1. https://dash.cloudflare.com → Workers & Pages → Create → Pages → **Direct Upload** → project
   name `digestai`. You can upload the local `site/dist` folder once to create it.
2. Custom domains → add `digestai.news` (Cloudflare walks you through the DNS records).
3. My Profile → API Tokens → Create Token → template **Edit Cloudflare Workers** (it covers Pages) →
   `CLOUDFLARE_API_TOKEN`. Account ID is on the Workers & Pages overview page → `CLOUDFLARE_ACCOUNT_ID`.

## 6. Push the configuration and run

```powershell
.\scripts\push-config.ps1 -Check     # shows what is set and missing, pushes nothing
.\scripts\push-config.ps1            # pushes secrets and variables to the repo
gh workflow run "Ingest and publish"
gh run watch
```

After the first run, open Supabase → SQL Editor and run the contents of `supabase/schema.sql`
once (indexes, event permissions, the editor view). Then run the workflow again.

## Checks when it is live

- https://digestai.news shows today's briefing and updates every 30 minutes.
- Supabase → Table Editor → `events` fills up as people read.
- Kit → Broadcasts shows a draft or sent email each morning at the configured hour.
- Search Console: add the property and submit `https://digestai.news/sitemap-index.xml` and
  `https://digestai.news/news-sitemap.xml`.
