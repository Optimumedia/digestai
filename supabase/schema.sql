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

-- 2. Lock EVERY table in the public schema down for the public keys (anon, authenticated),
--    including tables added later, then allow ONLY inserts into events.
--    The pipeline uses the direct Postgres connection string and is unaffected by RLS.
do $$
declare t record;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t.tablename);
    execute format('revoke all on public.%I from anon, authenticated', t.tablename);
  end loop;
  for t in select viewname from pg_views where schemaname = 'public' loop
    execute format('revoke all on public.%I from anon, authenticated', t.viewname);
  end loop;
  for t in select sequence_name from information_schema.sequences where sequence_schema = 'public' loop
    execute format('revoke all on sequence public.%I from anon, authenticated', t.sequence_name);
  end loop;
end $$;
-- Tables the pipeline creates in future get no public grants either.
alter default privileges for role postgres in schema public revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public revoke all on sequences from anon, authenticated;

grant insert on events to anon;
grant usage, select on sequence events_id_seq to anon;

drop policy if exists "public can log events" on events;
create policy "public can log events" on events
  for insert to anon
  with check (
    type in ('view', 'click_source', 'dwell', 'share', 'newsletter_click', 'save', 'follow', 'comment', 'push_on', 'listen')
    and value >= 0 and value <= 3600
  );

-- 2b. Abuse limits on the public insert path. The publishable key is in every page, so anyone
--     can call the insert endpoint; these checks keep a script from flooding the table or
--     forging engagement for a story: at most 30 events per session per minute, only known
--     stories, only recent timestamps, bounded payload sizes.
create or replace function public.events_guard() returns trigger
  language plpgsql security definer set search_path = public as $$
begin
  -- Page views, listens and alert sign-ups happen on pages that are not stories: story_id may be
  -- empty, but a story_id that is given must exist.
  if new.story_id is not null and not exists (select 1 from stories s where s.id = new.story_id) then
    raise exception 'unknown story';
  end if;
  if new.created_at is null or new.created_at > now() + interval '5 minutes' or new.created_at < now() - interval '1 day' then
    new.created_at := now();
  end if;
  if length(coalesce(new.session, '')) > 40 or length(coalesce(new.path, '')) > 200
     or length(coalesce(new.visitor, '')) > 40 or length(coalesce(new.source, '')) > 60 then
    raise exception 'payload too large';
  end if;
  if (select count(*) from events e where e.session = new.session and e.created_at > now() - interval '1 minute') >= 30 then
    raise exception 'too many events';
  end if;
  return new;
end $$;
drop trigger if exists events_guard on events;
create trigger events_guard before insert on events for each row execute function public.events_guard();

-- 2c. Retention: events older than 90 days are not used by the ranker and are removed daily
--     by the pipeline (rank.py); this index makes that cheap.
create index if not exists events_session_created_idx on events (session, created_at desc);

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

-- 3. Browser push alerts: the site inserts a subscription (endpoint + keys) through the same
--    anonymous path as events. Nothing is readable back; the pipeline prunes dead endpoints.
create table if not exists push_subscriptions (
  id serial primary key,
  endpoint text not null unique,
  p256dh varchar(200) not null,
  auth varchar(100) not null,
  topics text,
  failures integer not null default 0,
  created_at timestamptz not null default now(),
  last_ok_at timestamptz
);
alter table push_subscriptions alter column failures set default 0;
alter table push_subscriptions alter column created_at set default now();
alter table push_subscriptions enable row level security;
revoke all on push_subscriptions from anon, authenticated;
grant insert on push_subscriptions to anon;
grant usage, select on sequence push_subscriptions_id_seq to anon;
drop policy if exists "public can subscribe to alerts" on push_subscriptions;
create policy "public can subscribe to alerts" on push_subscriptions
  for insert to anon
  with check (
    endpoint like 'https://%' and length(endpoint) <= 1000
    and length(p256dh) between 60 and 200 and length(auth) between 10 and 100
    and (topics is null or length(topics) <= 2000)
    and coalesce(failures, 0) = 0
  );
