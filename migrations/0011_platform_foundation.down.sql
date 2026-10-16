-- 0011_platform_foundation.down.sql
--
-- Removes the Phase 4 foundation: the application tables, the jobs queue, the
-- gitscout_app role, the repository configuration columns and the legacy alias view.
-- Restores the 0010 versions of ensure_repo_schema() and ensure_outreach_view().
-- Mirrored data is untouched.

begin;

drop view if exists "Defendermate2".final_outreach_users_trivy;

drop table if exists gitscout.exports;
drop table if exists gitscout.exclusions;
drop table if exists gitscout.audit_log;
drop table if exists gitscout.memberships;
drop table if exists gitscout.app_users;
drop table if exists gitscout.app_settings;
drop table if exists gitscout.jobs;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'gitscout_app') then
        execute 'drop owned by gitscout_app';
        execute 'drop role gitscout_app';
    end if;
end
$$;

alter table gitscout.repositories
    drop constraint if exists repositories_collections_check,
    drop constraint if exists repositories_schedule_check,
    drop column if exists enabled,
    drop column if exists collections,
    drop column if exists schedule_cron,
    drop column if exists timezone,
    drop column if exists settings,
    drop column if exists checkpoint,
    drop column if exists last_scheduled_at,
    drop column if exists last_success_at,
    drop column if exists last_job_id,
    drop column if exists added_by,
    drop column if exists added_at;

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

    if to_regnamespace(format('%I', s)) is null then
        execute format('create schema %I', s);
    end if;
    execute format('revoke all on schema %I from anon, authenticated', s);
    execute format('grant usage on schema %I to gitscout_writer', s);

    foreach t in array array['threads', 'comments', 'interactions'] loop
        if to_regclass(format('%I.%I', s, t)) is null then
            execute format('create table %I.%I (like %I.%I including all)',
                           s, t, 'Defendermate-Github1-Trivy', t);
            execute format('alter table %I.%I enable row level security', s, t);
            execute format('create policy gitscout_writer_rw on %I.%I for all to gitscout_writer '
                           'using (true) with check (true)', s, t);
            execute format('grant select, insert, update on %I.%I to gitscout_writer', s, t);
        end if;
    end loop;
    if not exists (select 1 from pg_constraint
                    where conrelid = format('%I.comments', s)::regclass and contype = 'f') then
        execute format('alter table %I.comments add foreign key (repo, kind, number) '
                       'references %I.threads (repo, kind, number) on delete cascade', s, s);
    end if;

    perform gitscout.rebuild_union_views();
    return s;
end
$$;

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
begin
    select owner, name into r_owner, r_name
    from gitscout.repositories where repo = repo_full;
    if r_name is null then
        raise exception 'repository % is not registered (run gitscout repo-info)', repo_full;
    end if;
    view_name := gitscout.repo_schema_name(repo_full) || '-Final';

    execute format(
        'create or replace view %I.%I with (security_invoker = true) as '
        'select * from %I.final_outreach_users '
        'where repository_owner = %L and repository_name = %L',
        'Defendermate2', view_name, 'Defendermate2', r_owner, r_name);
    execute format('revoke all on %I.%I from anon, authenticated', 'Defendermate2', view_name);
    return view_name;
end;
$$;

commit;
