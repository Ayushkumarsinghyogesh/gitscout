-- 0011_platform_foundation.sql  (Phase 4: repository configuration, job state, app tables)
--
-- The pieces every later phase stands on. Nothing here changes a row of mirrored data.
--
--   1. gitscout.repositories gains its configuration: enabled, collections, schedule
--      (5-field cron, evaluated in the repository's own time zone), per-repo settings,
--      a checkpoint, and the last success. The TOML profile becomes a one-time seed.
--   2. gitscout.jobs is the durable queue and the run history in one table. A partial
--      unique index allows one queued-or-running job per (repository, kind), which is
--      what makes overlapping runs impossible rather than merely unlikely.
--   3. gitscout.app_users / memberships / audit_log / exclusions / exports /
--      app_settings: the application's own tables. app_users.user_id is the Supabase
--      Auth id but carries no foreign key to auth.users, on purpose: the schema stays
--      portable to plain Postgres.
--   4. Role gitscout_app: what the dashboard connects as. Read everything, write only
--      the application tables and the configuration columns of repositories, delete
--      nothing. Created NOLOGIN; the password is set out of band, like the writer's.
--   5. gitscout.ensure_repo_schema() also grants the app role on a new repository's
--      schema; gitscout.ensure_outreach_view() also creates the legacy alias
--      "Defendermate2".final_outreach_users_<slug> next to ...-<Name>-Final, so both
--      names work. Any future rebuild of the materialized view must drop BOTH views.
--
-- Requires 0010. Rollback: 0011_platform_foundation.down.sql

begin;

-- ------------------------------------------------------------ 1. repositories
alter table gitscout.repositories
    add column if not exists enabled           boolean     not null default true,
    add column if not exists collections       text[]      not null default '{issues,prs,discussions,forks,commits}',
    add column if not exists schedule_cron     text        not null default '0 0 * * *',
    add column if not exists timezone          text        not null default 'Asia/Kolkata',
    add column if not exists settings          jsonb       not null default '{}'::jsonb,
    add column if not exists checkpoint        jsonb       not null default '{}'::jsonb,
    add column if not exists last_scheduled_at timestamptz,
    add column if not exists last_success_at   timestamptz,
    add column if not exists last_job_id       bigint,
    add column if not exists added_by          uuid,
    add column if not exists added_at          timestamptz not null default now();

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'repositories_collections_check') then
        alter table gitscout.repositories add constraint repositories_collections_check
            check (collections <@ '{issues,prs,discussions,forks,commits,stars}'::text[]);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'repositories_schedule_check') then
        alter table gitscout.repositories add constraint repositories_schedule_check
            check (schedule_cron ~ '^\S+ \S+ \S+ \S+ \S+$');
    end if;
end
$$;

comment on column gitscout.repositories.enabled is
    'false: the scheduler skips it and the dashboard shows it paused; data stays';
comment on column gitscout.repositories.collections is
    'what a run collects: issues, prs, discussions, forks, commits (stars needs admin rights on the repo)';
comment on column gitscout.repositories.schedule_cron is
    '5-field cron (minute hour day month weekday) evaluated in `timezone`; default: every night at 00:00';
comment on column gitscout.repositories.settings is
    'per-repository knobs, e.g. {"max": 2000, "limit": 2000, "comments": 20}';
comment on column gitscout.repositories.checkpoint is
    'the worker''s last cursors and high-water marks, copied from its local store after every run, so a lost worker disk resumes instead of re-backfilling';
comment on column gitscout.repositories.last_scheduled_at is
    'the minute the scheduler last enqueued a run for; prevents enqueuing twice in one minute';

-- ---------------------------------------------------------------------- 2. jobs
create table if not exists gitscout.jobs (
    id              bigserial   primary key,
    repo            text        not null references gitscout.repositories (repo) on delete cascade,
    kind            text        not null check (kind in ('incremental', 'full', 'backfill', 'sweep', 'export')),
    trigger         text        not null check (trigger in ('schedule', 'manual', 'retry', 'system')),
    requested_by    uuid,                                      -- app user, for manual runs
    status          text        not null default 'queued'
                                check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    priority        smallint    not null default 100,          -- lower runs first
    run_after       timestamptz not null default now(),
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    heartbeat_at    timestamptz,
    attempt         smallint    not null default 0,
    max_attempts    smallint    not null default 3,
    worker          text,                                      -- host/pid that ran it
    code_version    text,                                      -- git sha of the worker
    collections     text[]      not null default '{}',
    counters        jsonb       not null default '{}'::jsonb,  -- {"issues": {"discovered":.., "inserted":.., "updated":.., "skipped":.., "failed":..}, ...}
    checkpoint      jsonb       not null default '{}'::jsonb,  -- cursors at the end of the run
    rate            jsonb       not null default '{}'::jsonb,  -- {"points_used":.., "remaining":.., "reset_at":..}
    error           text,                                      -- sanitized: no DSN, no token
    export_id       bigint,
    parent_job_id   bigint      references gitscout.jobs (id) on delete set null
);
comment on table gitscout.jobs is
    'the durable queue and the run history: a row is created queued, claimed by one worker, and ends succeeded, failed or cancelled';

-- One queued-or-running job per (repository, kind). The second enqueue fails on this
-- index, which is the overlap protection; everything else is bookkeeping.
create unique index if not exists jobs_one_active_per_repo_kind
    on gitscout.jobs (repo, kind) where status in ('queued', 'running');
create index if not exists jobs_queue_idx
    on gitscout.jobs (priority, run_after, id) where status = 'queued';
create index if not exists jobs_running_idx
    on gitscout.jobs (heartbeat_at) where status = 'running';
create index if not exists jobs_repo_created_idx
    on gitscout.jobs (repo, created_at desc);

-- --------------------------------------------------- 3. application tables
create table if not exists gitscout.app_users (
    user_id       uuid        primary key,                     -- Supabase Auth user id (no FK: portable)
    email         text        not null unique,
    role          text        not null check (role in ('platform_admin', 'client_admin', 'client_viewer')),
    display_name  text,
    created_at    timestamptz not null default now(),
    created_by    uuid,
    disabled_at   timestamptz
);
comment on table gitscout.app_users is
    'application roles. Rows are written only by the server-side admin path, never from signup metadata';

create table if not exists gitscout.memberships (
    user_id     uuid        not null references gitscout.app_users (user_id) on delete cascade,
    repo        text        not null references gitscout.repositories (repo) on delete cascade,
    role        text        not null check (role in ('client_admin', 'client_viewer')),
    granted_by  uuid,
    granted_at  timestamptz not null default now(),
    primary key (user_id, repo)
);
comment on table gitscout.memberships is
    'which repositories a client user may see; platform_admin needs no rows here';

create table if not exists gitscout.audit_log (
    id           bigserial   primary key,
    at           timestamptz not null default now(),
    actor_id     uuid,
    actor_email  text,
    action       text        not null,                         -- e.g. repo.enable, job.enqueue, record.delete
    target_kind  text,
    target_id    text,
    repo         text,
    affected     integer,
    details      jsonb       not null default '{}'::jsonb,
    result       text        not null default 'ok'
);
create index if not exists audit_log_at_idx on gitscout.audit_log (at desc);
comment on table gitscout.audit_log is 'append-only; nobody has delete or update on it';

create table if not exists gitscout.exclusions (
    id          bigserial   primary key,
    repo        text        not null references gitscout.repositories (repo) on delete cascade,
    kind        text        not null check (kind in ('person', 'thread')),
    key         text        not null,                          -- login, or '<section>/<number>'
    reason      text,
    created_by  uuid,
    created_at  timestamptz not null default now(),
    removed_at  timestamptz,
    unique (repo, kind, key)
);
comment on table gitscout.exclusions is
    'manual decisions a sync must never undo: the outreach dataset leaves these out even after the source row is re-synced';

create table if not exists gitscout.exports (
    id            bigserial   primary key,
    repo          text        references gitscout.repositories (repo) on delete set null,
    job_id        bigint      references gitscout.jobs (id) on delete set null,
    format        text        not null check (format in ('csv', 'json')),
    storage_path  text        not null,                        -- object key in the private bucket
    row_count     integer,
    byte_size     bigint,
    filters       jsonb       not null default '{}'::jsonb,
    generated_at  timestamptz not null default now(),
    generated_by  uuid,
    expires_at    timestamptz
);
create index if not exists exports_repo_generated_idx on gitscout.exports (repo, generated_at desc);

create table if not exists gitscout.app_settings (
    key         text        primary key,
    value       jsonb       not null,
    updated_at  timestamptz not null default now(),
    updated_by  uuid
);
insert into gitscout.app_settings (key, value) values
    ('export_retention_days', '90'::jsonb),
    ('stale_job_minutes',     '10'::jsonb),
    ('alert_emails',          '[]'::jsonb)
on conflict (key) do nothing;

-- ------------------------------------------------------------ 4. role gitscout_app
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'gitscout_app') then
        create role gitscout_app nologin;
    end if;
end
$$;

grant usage on schema gitscout, "Defendermate2", "Defendermate-Github1-Trivy" to gitscout_app;
grant select on all tables in schema gitscout to gitscout_app;                      -- tables and views
grant select on all tables in schema "Defendermate-Github1-Trivy" to gitscout_app;  -- the union views are security_invoker
grant select on "Defendermate2".final_outreach_users,
                "Defendermate2"."Defendermate-Github1-Trivy-Final" to gitscout_app;
grant insert, update on gitscout.jobs, gitscout.app_users, gitscout.memberships,
                       gitscout.exclusions, gitscout.exports, gitscout.app_settings to gitscout_app;
grant insert on gitscout.audit_log to gitscout_app;
grant insert on gitscout.repositories to gitscout_app;
grant update (enabled, collections, schedule_cron, timezone, settings, added_by)
    on gitscout.repositories to gitscout_app;
grant usage, select on all sequences in schema gitscout to gitscout_app;
grant execute on function gitscout.ensure_repo_schema(text),
                          gitscout.ensure_outreach_view(text) to gitscout_app;

-- the worker: claims jobs, records runs, writes exports, reads decisions and settings
grant select, insert, update on gitscout.jobs, gitscout.exports to gitscout_writer;
grant insert on gitscout.audit_log to gitscout_writer;
grant select on gitscout.exclusions, gitscout.app_settings, gitscout.app_users to gitscout_writer;
grant usage, select on all sequences in schema gitscout to gitscout_writer;

-- RLS on every new table, same pattern as the rest: zero policies = nobody, one
-- policy per role that the grants above already limit.
do $$
declare
    t text;
begin
    foreach t in array array['jobs', 'app_users', 'memberships', 'audit_log',
                             'exclusions', 'exports', 'app_settings'] loop
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
    -- the app may read the mirror and change repository configuration
    foreach t in array array['gitscout.repositories', 'gitscout.people', 'gitscout.person_emails',
                             'gitscout.sync_runs', '"Defendermate-Github1-Trivy".threads',
                             '"Defendermate-Github1-Trivy".comments',
                             '"Defendermate-Github1-Trivy".interactions'] loop
        if not exists (select 1 from pg_policy where polrelid = t::regclass and polname = 'gitscout_app_rw') then
            execute format('create policy gitscout_app_rw on %s for all to gitscout_app '
                           'using (true) with check (true)', t);
        end if;
    end loop;
end
$$;
revoke all on gitscout.jobs, gitscout.app_users, gitscout.memberships, gitscout.audit_log,
              gitscout.exclusions, gitscout.exports, gitscout.app_settings from anon, authenticated;

-- ------------------------------------- 5. functions: new repositories get the app role
create or replace function gitscout.ensure_repo_schema(repo_full text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    s text;
    t text;
begin
    s := gitscout.repo_schema_name(repo_full);
    if s is null then
        raise exception 'repository % is not registered (run gitscout repo-info)', repo_full;
    end if;

    -- The names carry upper-case letters and hyphens: always through %I.
    if to_regnamespace(format('%I', s)) is null then
        execute format('create schema %I', s);
    end if;
    execute format('revoke all on schema %I from anon, authenticated', s);
    execute format('grant usage on schema %I to gitscout_writer, gitscout_app', s);

    foreach t in array array['threads', 'comments', 'interactions'] loop
        if to_regclass(format('%I.%I', s, t)) is null then
            -- Trivy's tables are the template: same columns, indexes, checks, defaults.
            execute format('create table %I.%I (like %I.%I including all)',
                           s, t, 'Defendermate-Github1-Trivy', t);
            execute format('alter table %I.%I enable row level security', s, t);
            execute format('create policy gitscout_writer_rw on %I.%I for all to gitscout_writer '
                           'using (true) with check (true)', s, t);
            execute format('create policy gitscout_app_rw on %I.%I for all to gitscout_app '
                           'using (true) with check (true)', s, t);
            execute format('grant select, insert, update on %I.%I to gitscout_writer', s, t);
            execute format('grant select on %I.%I to gitscout_app', s, t);
        end if;
    end loop;
    -- LIKE does not copy foreign keys.
    if not exists (select 1 from pg_constraint
                    where conrelid = format('%I.comments', s)::regclass and contype = 'f') then
        execute format('alter table %I.comments add foreign key (repo, kind, number) '
                       'references %I.threads (repo, kind, number) on delete cascade', s, s);
    end if;

    perform gitscout.rebuild_union_views();
    return s;
end
$$;

-- Two views per registered repository, same rows:
--   "Defendermate2"."Defendermate-Github1-<Name>-Final"   (the name in use)
--   "Defendermate2".final_outreach_users_<slug>          (the original convention)
create or replace function gitscout.ensure_outreach_view(repo_full text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    r_owner   text;
    r_name    text;
    view_name text;
    alias_name text;
begin
    select owner, name into r_owner, r_name
    from gitscout.repositories where repo = repo_full;
    if r_name is null then
        raise exception 'repository % is not registered (run gitscout repo-info)', repo_full;
    end if;
    view_name  := gitscout.repo_schema_name(repo_full) || '-Final';
    alias_name := 'final_outreach_users_' || lower(regexp_replace(r_name, '[^A-Za-z0-9]+', '_', 'g'));

    execute format(
        'create or replace view %I.%I with (security_invoker = true) as '
        'select * from %I.final_outreach_users '
        'where repository_owner = %L and repository_name = %L',
        'Defendermate2', view_name, 'Defendermate2', r_owner, r_name);
    execute format(
        'create or replace view %I.%I with (security_invoker = true) as select * from %I.%I',
        'Defendermate2', alias_name, 'Defendermate2', view_name);
    execute format('revoke all on %I.%I, %I.%I from anon, authenticated',
                   'Defendermate2', view_name, 'Defendermate2', alias_name);
    execute format('grant select on %I.%I, %I.%I to gitscout_app',
                   'Defendermate2', view_name, 'Defendermate2', alias_name);
    return view_name;
end;
$$;
revoke execute on function gitscout.ensure_outreach_view(text) from public, anon, authenticated;

-- ---------------------------------------------------------------- 6. seed trivy
select gitscout.ensure_outreach_view('aquasecurity/trivy');

update gitscout.repositories
   set enabled = true,
       schedule_cron = '0 0 * * *',
       timezone = 'Asia/Kolkata',
       settings = '{"max": 2000, "limit": 2000}'::jsonb,
       last_success_at = (select max(started_at) from gitscout.sync_runs where status = 'ok')
 where repo = 'aquasecurity/trivy';

commit;
