-- Run once in the Supabase SQL editor AFTER the pipeline has created its tables
-- (the pipeline creates sources/articles/stories/events/runs on first run).
--
-- 1. Indexes the pipeline relies on.
create index if not exists articles_status_idx on articles (status);
create index if not exists articles_story_idx on articles (story_id);
create index if not exists articles_created_idx on articles (created_at desc);
create index if not exists stories_updated_idx on stories (updated_at desc);
create index if not exists events_created_idx on events (created_at desc);
create index if not exists events_article_idx on events (article_id);

-- 2. Lock everything down for the public (anon) key, then allow ONLY inserts into events.
--    The pipeline uses the direct Postgres connection string and is unaffected by RLS.
alter table sources  enable row level security;
alter table articles enable row level security;
alter table stories  enable row level security;
alter table runs     enable row level security;
alter table events   enable row level security;

revoke all on sources, articles, stories, runs from anon, authenticated;
revoke all on events from anon, authenticated;
grant insert on events to anon;
grant usage, select on sequence events_id_seq to anon;

drop policy if exists "public can log events" on events;
create policy "public can log events" on events
  for insert to anon
  with check (
    type in ('view', 'click_source', 'dwell', 'share', 'newsletter_click', 'save', 'follow', 'comment')
    and value >= 0 and value <= 3600
  );

-- 3. Editorial helpers for Supabase Studio: a view of what is live, ordered like the site.
create or replace view live_stories as
  select s.id, s.slug, s.headline, s.category, s.importance, s.score, s.article_count,
         s.pinned, s.status, s.updated_at
  from stories s
  where s.status = 'published'
  order by s.score desc;

-- To unpublish a story:   update stories set status = 'unpublished' where id = 123;
-- To pin to the front page: update stories set pinned = true where id = 123;
-- To drop one article:     update articles set status = 'unpublished' where id = 456;
