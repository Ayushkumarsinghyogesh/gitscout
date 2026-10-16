-- 0013_observability.sql  (Phase 6: alerts, worker liveness, run history, repository health)
--
-- Nothing here changes a row of mirrored data.
--
--   1. gitscout.alerts: every alert the worker raised, sent or not (no recipients or no
--      mail key configured still leaves a row). The key column is what the cooldown
--      looks up: one email per key per alert_cooldown_minutes, however often it fires.
--   2. gitscout.workers: one row per worker process, seen_at refreshed every tick and
--      every heartbeat. "Is the worker alive" is a query, not a guess.
--   3. Views (security_invoker, so callers keep their own privileges):
--        gitscout.run_history  - one row per job with duration and the headline counts
--        gitscout.repo_health  - per repository: last success, last job, consecutive
--                                failures, active job, overdue flag
--   4. app_settings keys: alert_cooldown_minutes 360, overdue_hours 26,
--      rate_alert_floor 250, worker_silent_minutes 15.
--
-- Requires 0011. Rollback: 0013_observability.down.sql

begin;

-- ------------------------------------------------------------------- 1. alerts
create table if not exists gitscout.alerts (
    id          bigserial   primary key,
    at          timestamptz not null default now(),
    key         text        not null,                 -- e.g. job_failed:aquasecurity/trivy:incremental
    severity    text        not null check (severity in ('info', 'warning', 'critical')),
    repo        text,
    job_id      bigint,
    subject     text        not null,
    body        text        not null default '',
    recipients  text[]      not null default '{}',
    channel     text        not null default 'none' check (channel in ('email', 'none')),
    sent_at     timestamptz,
    error       text
);
create index if not exists alerts_key_at_idx on gitscout.alerts (key, at desc);
create index if not exists alerts_at_idx on gitscout.alerts (at desc);

-- ------------------------------------------------------------------ 2. workers
create table if not exists gitscout.workers (
    name            text        primary key,          -- host:pid
    version         text,
    started_at      timestamptz not null default now(),
    seen_at         timestamptz not null default now(),
    jobs_run        integer     not null default 0,
    current_job_id  bigint,
    stopped_at      timestamptz
);

-- -------------------------------------------------------------------- 3. views
create or replace view gitscout.run_history with (security_invoker = true) as
select j.id, j.repo, j.kind, j.trigger, j.status, j.attempt, j.max_attempts, j.parent_job_id,
       j.created_at, j.started_at, j.finished_at,
       extract(epoch from (coalesce(j.finished_at, j.heartbeat_at) - j.started_at))::integer as seconds,
       j.worker, j.code_version,
       (j.counters #>> '{interactions,discovered}')::integer as interactions_new,
       (j.counters #>> '{people,new}')::integer             as people_new,
       (j.counters #>> '{emails,new}')::integer             as emails_new,
       (j.counters #>> '{push,rows}')::integer              as rows_pushed,
       (j.rate ->> 'points_used')::integer                  as points_used,
       (j.rate ->> 'remaining')::integer                    as points_remaining,
       split_part(j.error, E'\n', 1)                        as error
from gitscout.jobs j;

create or replace view gitscout.repo_health with (security_invoker = true) as
with last_ok as (
    select repo, max(id) as id from gitscout.jobs
    where status = 'succeeded' and kind in ('incremental', 'full', 'backfill') group by repo
), fails as (
    select j.repo, count(*) as n from gitscout.jobs j left join last_ok o using (repo)
    where j.status = 'failed' and j.kind in ('incremental', 'full', 'backfill')
      and j.id > coalesce(o.id, 0) group by j.repo
), last_job as (
    select distinct on (repo) repo, id, kind, status, finished_at, split_part(error, E'\n', 1) as error
    from gitscout.jobs order by repo, id desc
), active as (
    select distinct on (repo) repo, id, status, started_at, heartbeat_at
    from gitscout.jobs where status in ('queued', 'running') order by repo, id
), overdue as (
    select coalesce((value #>> '{}')::numeric, 26) as hours
    from gitscout.app_settings where key = 'overdue_hours'
)
select r.repo, r.enabled, r.schedule_cron, r.timezone, r.added_at, r.last_success_at,
       round(extract(epoch from (now() - r.last_success_at)) / 3600.0, 1) as hours_since_success,
       coalesce(f.n, 0)  as consecutive_failures,
       l.id as last_job_id, l.kind as last_job_kind, l.status as last_job_status,
       l.finished_at as last_job_finished_at, l.error as last_job_error,
       a.id as active_job_id, a.status as active_job_status, a.heartbeat_at as active_heartbeat_at,
       (r.enabled and coalesce(r.last_success_at, r.added_at)
            < now() - make_interval(hours => coalesce((select hours from overdue), 26)::integer)) as overdue
from gitscout.repositories r
left join fails f using (repo)
left join last_job l using (repo)
left join active a using (repo);

-- ---------------------------------------------------------------- 4. settings
insert into gitscout.app_settings (key, value) values
    ('alert_cooldown_minutes', '360'::jsonb),
    ('overdue_hours',          '26'::jsonb),
    ('rate_alert_floor',       '250'::jsonb),
    ('worker_silent_minutes',  '15'::jsonb)
on conflict (key) do nothing;

-- ------------------------------------------------------------- 5. privileges
grant select, insert, update on gitscout.alerts, gitscout.workers to gitscout_writer;
grant select on gitscout.alerts, gitscout.workers to gitscout_app;
grant select on gitscout.run_history, gitscout.repo_health to gitscout_writer, gitscout_app;
grant usage, select on all sequences in schema gitscout to gitscout_writer, gitscout_app;

do $$
declare
    t text;
begin
    foreach t in array array['alerts', 'workers'] loop
        execute format('alter table gitscout.%I enable row level security', t);
        if not exists (select 1 from pg_policy where polrelid = format('gitscout.%I', t)::regclass
                                                  and polname = 'gitscout_app_rw') then
            execute format('create policy gitscout_app_rw on gitscout.%I for all to gitscout_app '
                           'using (true) with check (true)', t);
        end if;
        if not exists (select 1 from pg_policy where polrelid = format('gitscout.%I', t)::regclass
                                                  and polname = 'gitscout_writer_rw') then
            execute format('create policy gitscout_writer_rw on gitscout.%I for all to gitscout_writer '
                           'using (true) with check (true)', t);
        end if;
    end loop;
end
$$;
revoke all on gitscout.alerts, gitscout.workers, gitscout.run_history, gitscout.repo_health
    from anon, authenticated;

commit;
