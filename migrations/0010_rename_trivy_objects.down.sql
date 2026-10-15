-- 0010_rename_trivy_objects.down.sql
--
-- Restores defendermate_trivy_gitscout and "Defendermate2".final_outreach_users_trivy,
-- and the 0008a / 0005 naming functions (defendermate_<name>_gitscout,
-- final_outreach_users_<name>).

begin;

alter schema "Defendermate-Github1-Trivy" rename to defendermate_trivy_gitscout;
alter view "Defendermate2"."Defendermate-Github1-Trivy-Final" rename to final_outreach_users_trivy;

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
                  where nspname ~ '^defendermate_.+_gitscout$' order by nspname loop
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
create or replace function gitscout.repo_schema_name(repo_full text)
returns text
language sql
stable
set search_path = ''
as $$
    select 'defendermate_' || lower(regexp_replace(name, '[^A-Za-z0-9]+', '_', 'g')) || '_gitscout'
      from gitscout.repositories where repo = repo_full
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
    -- Two registered repositories with the same name: the alphabetically first keeps
    -- the short schema name, the other gets owner_name.
    if exists (select 1 from gitscout.repositories r
                where r.repo <> repo_full and r.repo < repo_full
                  and gitscout.repo_schema_name(r.repo) = s) then
        select 'defendermate_' || lower(regexp_replace(owner || '_' || name, '[^A-Za-z0-9]+', '_', 'g'))
               || '_gitscout' into s
          from gitscout.repositories where repo = repo_full;
    end if;

    if to_regnamespace(s) is null then
        execute format('create schema %I', s);
    end if;
    execute format('revoke all on schema %I from anon, authenticated', s);
    execute format('grant usage on schema %I to gitscout_writer', s);

    foreach t in array array['threads', 'comments', 'interactions'] loop
        if to_regclass(format('%I.%I', s, t)) is null then
            -- Trivy's tables are the template: same columns, indexes, checks, defaults.
            execute format('create table %I.%I (like defendermate_trivy_gitscout.%I including all)', s, t, t);
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
create or replace function gitscout.ensure_outreach_view(repo_full text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    r_owner text;
    r_name  text;
    slug    text;
    clashes integer;
    view_name text;
begin
    select owner, name into r_owner, r_name
    from gitscout.repositories where repo = repo_full;
    if r_name is null then
        raise exception 'repository % is not registered (run gitscout repo-info)', repo_full;
    end if;

    slug := lower(regexp_replace(r_name, '[^A-Za-z0-9]+', '_', 'g'));
    select count(*) into clashes from gitscout.repositories
     where repo <> repo_full
       and lower(regexp_replace(name, '[^A-Za-z0-9]+', '_', 'g')) = slug;
    if clashes > 0 then
        slug := lower(regexp_replace(r_owner || '_' || r_name, '[^A-Za-z0-9]+', '_', 'g'));
    end if;
    view_name := 'final_outreach_users_' || slug;

    execute format(
        'create or replace view %I.%I with (security_invoker = true) as '
        'select * from %I.final_outreach_users '
        'where repository_owner = %L and repository_name = %L',
        'Defendermate2', view_name, 'Defendermate2', r_owner, r_name);
    execute format('revoke all on %I.%I from anon, authenticated', 'Defendermate2', view_name);
    return view_name;
end;
$$;


select gitscout.rebuild_union_views();
select gitscout.ensure_outreach_view('aquasecurity/trivy');

commit;
