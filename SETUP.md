# Going live

## To do (Martin)

- [ ] **Kit** newsletter account, form and v4 API key → `.env` → `scripts\push-config.ps1` (section 3). Turns on the 07:00 daily email; everything else is wired.
- [ ] **Bluesky** (optional): create an account for Digest AI, Settings → App passwords → create one; put `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in `.env`. Then ask for the auto-poster to be built. Telegram channel is the alternative: create a bot with @BotFather, put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL` in `.env`.
- [ ] **Google News Publisher Center**: https://publishercenter.google.com → add digestai.news as a publication (uses the Search Console verification already in place).
- [x] Search Console sitemaps submitted (11 Sep).
- [x] giscus installed (11 Sep).
- [x] Supabase connected (11 Sep).

## The site already runs with no accounts at all

The repository is public at https://github.com/Optimumedia/digestai and the workflow runs every
30 minutes. Without any secret it uses free, sign-up-free substitutes:

| Need | Zero-signup mode (now) | Upgrade (when you add the account) |
|---|---|---|
| Hosting | GitHub Pages, deployed by the workflow | Cloudflare Pages (unmetered bandwidth) |
| Database | SQLite kept in the Actions cache, daily backup artifact | Supabase Postgres + reader events |
| Summaries | Qwen 2.5 3B running on the runner (10 articles per run) | Gemini (done; free tier is only 20/day per model, used for top sources) and **Groq** (14,400/day free: every article gets a proper summary) |
| Unpublish | edit `pipeline/digest/moderation.yaml` on GitHub | Supabase Studio |
| Newsletter | none (the `/today` page and RSS exist) | Kit |
| Comments | none | giscus app install (one click) |

## The one step to make digestai.news show the new site (5 min)

Your DNS is already on Cloudflare. In the Cloudflare dashboard → digestai.news → DNS:

1. Delete the current records for `digestai.news` (`@`) and `www` that point at the old site.
2. Add `CNAME` `@` → `optimumedia.github.io`, proxy status **DNS only** (grey cloud).
3. Add `CNAME` `www` → `optimumedia.github.io`, **DNS only**.

Within an hour GitHub issues the certificate and https://digestai.news serves the new site.
(The old site disappears at that moment, so do this when you are ready.)

## Upgrades: each is a sign-up plus one value in `.env`

Copy `.env.example` to `.env` at the repo root, fill in what you have, then run
`.\scripts\push-config.ps1`. Do them in any order; each one switches on by itself.

## 1. Supabase (10 min)

1. https://supabase.com → New project. Name `digestai`, any region near your readers, note the
   database password.
2. Project Settings → Database → Connection string → URI, **Session** mode. Put it in `.env` as
   `DATABASE_URL` (replace `[YOUR-PASSWORD]`).
3. Project Settings → API. Copy **Project URL** → `PUBLIC_SUPABASE_URL` and the **anon public** key →
   `PUBLIC_SUPABASE_ANON_KEY`. This key is designed to be public; the SQL in step 6 limits it to
   inserting reader events.

## 2. Groq key (3 min, the most valuable remaining upgrade)

- https://console.groq.com/keys → sign in (Google or GitHub account works) → Create API Key →
  paste into `.env` as `GROQ_API_KEY`. Free, no card, 14,400 requests a day.
- Gemini is already configured. Its free tier turned out to be 20 requests a day per model, so
  it only covers the top sources; Groq covers everything else with a strong model (Llama 3.3 70B).

## 3. Kit newsletter (10 min)

1. https://kit.com → sign up for the free Newsletter plan.
2. Grow → Landing Pages & Forms → Create → Form → any inline template → name it "Digest AI daily".
   Publish → the form's **action URL** is what the site posts to. It looks like
   `https://app.kit.com/forms/1234567/subscriptions`. Put it in `.env` as `PUBLIC_KIT_FORM_URL`.
3. Settings → Developer → **API Keys (v4)** → Create → `KIT_API_KEY`.
4. Optional: `NEWSLETTER_HOUR_UTC=5` sends at 07:00 Central European winter time; change if you like.

## 4. Giscus comments (2 min)

Discussions are already enabled and the IDs are already set as repository variables. The one
remaining step is installing the giscus GitHub app, which only the repo owner can do:

https://github.com/apps/giscus → Install → choose the `digestai` repository.

Comments use the default **Announcements** category, so only the site creates threads.

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

After the first run with Supabase, open Supabase → SQL Editor and run the contents of
`supabase/schema.sql` once (indexes, event permissions, the editor view). The stories gathered
in zero-signup mode are not migrated; the pipeline refills within a day.

When Cloudflare Pages takes over hosting, change the two DNS records to what Cloudflare Pages
shows under Custom domains; GitHub Pages then simply stops being used.

## Checks when it is live

- https://digestai.news shows today's briefing and updates every 30 minutes.
- Supabase → Table Editor → `events` fills up as people read.
- Kit → Broadcasts shows a draft or sent email each morning at the configured hour.
- Search Console: add the property and submit `https://digestai.news/sitemap-index.xml` and
  `https://digestai.news/news-sitemap.xml`.
