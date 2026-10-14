-- 0008a_repo_schemas.sql  (Phase 7, structure only -- no data is dropped or copied)
--
-- 1. Trivy's repository-scoped tables (threads, comments, interactions) move into
--    their own schema, defendermate_trivy_gitscout. ALTER TABLE ... SET SCHEMA moves
--    the catalog entry only: no row is copied; every index, constraint, grant and RLS
--    policy travels with the table; every view keeps working because Postgres binds
--    views by OID, not by name. The shared tables -- people, person_emails,
--    repositories, sync_runs -- stay in gitscout: a person active in two
--    repositories is one person, not two rows to enrich twice.
-- 2. gitscout.threads_all / comments_all / interactions_all are UNION ALL views over
--    every repository schema; the ten gitscout views are repointed at them, so adding
--    a repository never touches a view by hand.
-- 3. gitscout.ensure_repo_schema(repo) onboards a new repository: schema + tables
--    from the trivy ones as a template, same RLS/grants/foreign key, union views
--    rebuilt. The push calls it for every registered repository.
--
-- Rollback: 0008_repo_schemas_and_outreach_v3.down.sql

begin;

create schema if not exists defendermate_trivy_gitscout;
revoke all on schema defendermate_trivy_gitscout from anon, authenticated;
grant usage on schema defendermate_trivy_gitscout to gitscout_writer;

alter table gitscout.threads      set schema defendermate_trivy_gitscout;
alter table gitscout.comments     set schema defendermate_trivy_gitscout;
alter table gitscout.interactions set schema defendermate_trivy_gitscout;

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
revoke execute on function gitscout.rebuild_union_views() from public, anon, authenticated;
select gitscout.rebuild_union_views();

-- The ten gitscout views were bound to trivy's tables by OID when the tables moved.
-- They are recreated from their canonical definitions, reading the union views, so
-- they cover every repository. Same columns, same order: CREATE OR REPLACE applies.
create or replace view gitscout.issues with (security_invoker = true) as
    select * from gitscout.threads_all where kind = 'issues';

create or replace view gitscout.pull_requests with (security_invoker = true) as
    select * from gitscout.threads_all where kind = 'prs';

create or replace view gitscout.discussions with (security_invoker = true) as
    select * from gitscout.threads_all where kind = 'discussions';

create or replace view gitscout.participation with (security_invoker = true) as
select repo, kind, number, login, author_type,
       min(role) as role
from (
    select repo, kind, number, author as login, author_type, 'author' as role
    from gitscout.threads_all where author is not null
    union all
    select repo, kind, number, author as login, author_type,
           case when comment_type = 'review' then 'reviewer' else 'commenter' end
    from gitscout.comments_all where author is not null
) p
group by repo, kind, number, login, author_type;

create or replace view gitscout.engagement with (security_invoker = true) as
select v.login,
       p.name,
       p.best_email,
       p.email_confidence,
       p.lead_score,
       p.company,
       p.location,
       v.role,
       t.kind      as section,
       t.repo,
       t.number,
       t.url,
       t.title,
       t.body      as description,
       t.state,
       t.closed,
       t.category,
       t.labels,
       t.assignees,
       t.created_at,
       t.closed_at,
       t.comment_count,
       t.is_answered,
       (select count(*) from gitscout.comments_all c
         where c.repo = v.repo and c.kind = v.kind
           and c.number = v.number and c.author = v.login) as own_comments
from gitscout.participation v
join gitscout.threads_all t
  on t.repo = v.repo and t.kind = v.kind and t.number = v.number
left join gitscout.people p on p.login = v.login;

create or replace view gitscout.contactable with (security_invoker = true) as
select login, name, best_email, email_source, email_confidence, lead_score, company,
       location, estimated_utc_offset, followers, signals,
       issues_opened, prs_opened, prs_reviewed, discussions_answered, last_seen_at
from gitscout.people
where best_email is not null
order by lead_score desc nulls last;

create or replace view gitscout.thread_detail with (security_invoker = true) as
select
    v.login,
    p.name,
    p.best_email,
    p.email_source,
    p.email_confidence,
    p.lead_score,
    p.company,
    p.location,
    p.estimated_utc_offset,
    p.activity_hours_utc,
    v.role,                                    -- author | commenter | reviewer
    t.kind            as section,              -- issues | prs | discussions
    t.repo,
    t.number,
    t.url,
    t.title,
    t.body            as description,          -- the full original post
    t.state,
    t.closed,
    t.created_at,
    t.updated_at,
    t.closed_at,
    t.labels,
    t.category,                                -- discussions
    t.category_emoji,
    t.assignees,                               -- issues and PRs
    t.milestone,
    t.author          as opened_by,
    t.author_association,
    t.comment_count,
    t.comments_stored,
    t.comments_truncated,
    t.reaction_count,
    t.is_answered,
    t.answer_author,
    t.participants,
    (select count(*) from gitscout.comments_all c
      where c.repo = t.repo and c.kind = t.kind and c.number = t.number
        and c.author = v.login)                as own_comments,
    (select jsonb_agg(jsonb_build_object(
                'position',      c.position,
                'type',          c.comment_type,
                'author',        c.author,
                'author_email',  cp.best_email,
                'author_name',   cp.name,
                'author_company', cp.company,
                'author_utc_offset', cp.estimated_utc_offset,
                'association',   c.author_association,
                'created_at',    c.created_at,
                'is_answer',     c.is_answer,
                'review_state',  c.review_state,
                'body',          c.body)
            order by c.comment_type, c.position)
       from gitscout.comments_all c
       left join gitscout.people cp on cp.login = c.author
      where c.repo = t.repo and c.kind = t.kind and c.number = t.number)
                                               as comments
from gitscout.participation v
join gitscout.threads_all t
  on t.repo = v.repo and t.kind = v.kind and t.number = v.number
left join gitscout.people p on p.login = v.login;

create or replace view gitscout.issue_detail with (security_invoker = true) as
    select * from gitscout.thread_detail where section = 'issues';

create or replace view gitscout.discussion_detail with (security_invoker = true) as
    select * from gitscout.thread_detail where section = 'discussions';

create or replace view gitscout.pr_detail with (security_invoker = true) as
    select * from gitscout.thread_detail where section = 'prs';

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
revoke execute on function gitscout.repo_schema_name(text) from public, anon, authenticated;
revoke execute on function gitscout.ensure_repo_schema(text) from public, anon, authenticated;
grant execute on function gitscout.ensure_repo_schema(text) to gitscout_writer;

commit;
