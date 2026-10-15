-- 0010_rename_trivy_objects.sql  (the names the outreach team asked for)
--
--   defendermate_trivy_gitscout                  ->  "Defendermate-Github1-Trivy"
--   "Defendermate2".final_outreach_users_trivy   ->  "Defendermate2"."Defendermate-Github1-Trivy-Final"
--
-- ALTER ... RENAME changes the catalog name only: no row moves; every index,
-- constraint, grant, RLS policy and dependent view keeps working, because Postgres
-- binds them by OID. The naming functions follow the new pattern, so the next
-- repository gets Defendermate-Github1-<Name> and ...-<Name>-Final the same way.
-- Upper-case letters and hyphens mean both names must always be double-quoted.
--
-- Requires 0008a. Rollback: 0010_rename_trivy_objects.down.sql

begin;

alter schema defendermate_trivy_gitscout rename to "Defendermate-Github1-Trivy";
alter view "Defendermate2".final_outreach_users_trivy rename to "Defendermate-Github1-Trivy-Final";

-- Defendermate-Github1-<Name>, or Defendermate-Github1-<Owner>-<Name> when an
-- earlier-registered repository already has the same name: the first keeps the short
-- name, so nothing that already exists has to move.
create or replace function gitscout.repo_schema_name(repo_full text)
returns text
language sql
stable
set search_path = ''
as $$
    select 'Defendermate-Github1-'
           || initcap(regexp_replace(
                  case when exists (select 1 from gitscout.repositories o
                                     where o.repo <> r.repo and o.repo < r.repo
                                       and lower(o.name) = lower(r.name))
                       then r.owner || '-' || r.name
                       else r.name end,
                  '[^A-Za-z0-9]+', '-', 'g'))
      from gitscout.repositories r
     where r.repo = repo_full
$$;

create or replace function gitscout.rebuild_union_views()
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    t     text;
    parts text;
    s     record;
begin
    foreach t in array array['threads', 'comments', 'interactions'] loop
        parts := null;
        for s in select nspname from pg_namespace
                  where nspname ~ '^Defendermate-Github1-' order by nspname loop
            if to_regclass(format('%I.%I', s.nspname, t)) is not null then
                parts := concat_ws(' union all ', parts,
                                   format('select * from %I.%I', s.nspname, t));
            end if;
        end loop;
        if parts is null then
            raise exception 'no repository schema holds a % table', t;
        end if;
        execute format('create or replace view gitscout.%I with (security_invoker = true) as %s',
                       t || '_all', parts);
        execute format('revoke all on gitscout.%I from anon, authenticated', t || '_all');
    end loop;
end
$$;

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
    execute format('grant usage on schema %I to gitscout_writer', s);

    foreach t in array array['threads', 'comments', 'interactions'] loop
        if to_regclass(format('%I.%I', s, t)) is null then
            -- Trivy's tables are the template: same columns, indexes, checks, defaults.
            execute format('create table %I.%I (like %I.%I including all)',
                           s, t, 'Defendermate-Github1-Trivy', t);
            execute format('alter table %I.%I enable row level security', s, t);
            execute format('create policy gitscout_writer_rw on %I.%I for all to gitscout_writer '
                           'using (true) with check (true)', s, t);
            execute format('grant select, insert, update on %I.%I to gitscout_writer', s, t);
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

-- One view per registered repository: "Defendermate2"."Defendermate-Github1-<Name>-Final",
-- named after the repository's schema.
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

-- Same definitions, re-created so they read the new names.
select gitscout.rebuild_union_views();
select gitscout.ensure_outreach_view('aquasecurity/trivy');

commit;
