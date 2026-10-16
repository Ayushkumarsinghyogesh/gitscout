-- 0014_exports.sql  (Phase 7: exports by date range, nightly dumps, retention)
--
-- Nothing here changes a row of mirrored data, and the outreach view keeps its columns.
--
--   1. gitscout.dataset_first_collected: when GitScout first put each (repository,
--      person) into the outreach dataset. An export filtered by "collected between"
--      reads this. The dataset's own first_seen_at / last_seen_at are GitHub activity
--      dates, which is the other filter an export offers.
--      Backfill for rows already there: the person's earliest interaction recorded by
--      GitScout, else when their profile was fetched, else now (all of it is the
--      2026-10 crawl).
--   2. gitscout.refresh_outreach() records newcomers after every refresh, so the date
--      is kept from now on without anything else changing.
--   3. gitscout.exports gains deleted_at: the retention sweep removes the file from
--      Storage and marks the row; the history of what was exported stays.
--   4. A private Storage bucket gitscout-exports (Supabase only; skipped on plain
--      Postgres).
--
-- Requires 0013. Rollback: 0014_exports.down.sql

begin;

-- ---------------------------------------------------- 1. first collected dates
create table if not exists gitscout.dataset_first_collected (
    repo          text        not null,
    login         text        not null,
    collected_at  timestamptz not null default now(),
    primary key (repo, login)
);
create index if not exists dataset_first_collected_at_idx
    on gitscout.dataset_first_collected (repo, collected_at);

insert into gitscout.dataset_first_collected (repo, login, collected_at)
select f.repo, f.login,
       coalesce(least(i.first_recorded, p.github_fetched_at), i.first_recorded, p.github_fetched_at, now())
from (select repository_owner || '/' || repository_name as repo, username as login
      from "Defendermate2".final_outreach_users) f
left join (select repo, login, min(first_recorded_at) as first_recorded
           from gitscout.interactions_all group by repo, login) i using (repo, login)
left join gitscout.people p on p.login = f.login
on conflict (repo, login) do nothing;

-- ------------------------------------------- 2. refresh records the newcomers
create or replace function gitscout.refresh_outreach()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    started timestamptz := clock_timestamp();
    total   integer;
begin
    -- CONCURRENTLY: readers keep the previous result for the whole refresh.
    refresh materialized view concurrently "Defendermate2".final_outreach_users;
    select count(*) into total from "Defendermate2".final_outreach_users;
    -- first time in the dataset = collected now; existing rows keep their date
    insert into gitscout.dataset_first_collected (repo, login)
    select repository_owner || '/' || repository_name, username
    from "Defendermate2".final_outreach_users
    on conflict (repo, login) do nothing;
    insert into gitscout.sync_runs (mode, people, seconds, status)
    values ('refresh', total, extract(epoch from clock_timestamp() - started), 'ok');
    return total;
end;
$$;
revoke execute on function gitscout.refresh_outreach() from public, anon, authenticated;
grant execute on function gitscout.refresh_outreach() to gitscout_writer;

-- ------------------------------------------------------- 3. export retention
alter table gitscout.exports add column if not exists deleted_at timestamptz;

-- --------------------------------------------------------- 4. storage bucket
do $$
begin
    if to_regclass('storage.buckets') is not null then
        execute $q$insert into storage.buckets (id, name, public)
                   values ('gitscout-exports', 'gitscout-exports', false)
                   on conflict (id) do nothing$q$;
    end if;
end
$$;

-- ------------------------------------------------------------- privileges
grant select on gitscout.dataset_first_collected to gitscout_writer, gitscout_app;
alter table gitscout.dataset_first_collected enable row level security;
do $$
begin
    if not exists (select 1 from pg_policy where polrelid = 'gitscout.dataset_first_collected'::regclass
                                              and polname = 'gitscout_app_rw') then
        create policy gitscout_app_rw on gitscout.dataset_first_collected
            for all to gitscout_app using (true) with check (true);
    end if;
    if not exists (select 1 from pg_policy where polrelid = 'gitscout.dataset_first_collected'::regclass
                                              and polname = 'gitscout_writer_rw') then
        create policy gitscout_writer_rw on gitscout.dataset_first_collected
            for all to gitscout_writer using (true) with check (true);
    end if;
end
$$;
revoke all on gitscout.dataset_first_collected from anon, authenticated;

commit;
