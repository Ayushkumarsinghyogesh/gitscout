-- gitscout -> Supabase mirror: the canonical shape
--
-- `gitscout supabase schema -o schema.sql`, then run it once in the Supabase SQL editor
-- of a NEW project. Idempotent: re-running adds what is missing and changes nothing
-- else. A project that already holds the mirror is carried forward by the numbered
-- files in migrations/ instead; this file is what those add up to (0008d, 2026-10-14).
--
-- Three places, by what the data is:
--
--   gitscout.*                          shared. One person is one row however many
--                                       repositories they are active in.
--       people                          one row per GitHub user
--       person_emails                   every address found for them, with provenance
--       repositories                    the registry: owner/name -> GitHub's stable id
--       sync_runs                       append-only log of each push and refresh
--
--   "Defendermate-Github1-<Name>".*      one schema per repository, created by
--                                       gitscout.ensure_repo_schema() on first push.
--       threads                         issues, pull requests and discussions
--       comments                        comments, reviews, replies, inline code comments
--       interactions                    forks, commits, stars: signals with no thread
--       ("Defendermate-Github1-Trivy" is created here and is the template the
--        function copies -- same columns, indexes, checks, RLS and grants.)
--
--   "Defendermate2".final_outreach_users          what the outreach team reads: one row
--                                                 per (repository, person), 42 columns,
--                                                 every thread they touched with its full
--                                                 description and their own comments inline,
--                                                 materialized, refreshed after each push
--   "Defendermate2"."Defendermate-Github1-<Name>-Final"   the same, one repository
--
-- and the way to read the mirror itself:
--
--   gitscout.threads_all / comments_all / interactions_all   every repository, one view
--   gitscout.issues / pull_requests / discussions           the three sections
--   gitscout.participation                                  who was involved in what
--   gitscout.engagement                                     person + email + thread + labels
--   gitscout.thread_detail (+ issue_/discussion_/pr_detail)  one row per (person, thread)
--                                                           with the whole conversation
--   gitscout.contactable                                    people with an email
--   gitscout.details_for(logins, sections) / people_in(repo, section, number)
--
-- Access: anon and authenticated get nothing -- no USAGE on any of these schemas, RLS
-- on every table with no policy for them, every view security_invoker. The only
-- non-superuser that can touch the mirror is gitscout_writer: select/insert/update on
-- the tables, execute on three functions, no delete, no DDL. Its password is set out of
-- band (`alter role gitscout_writer login password '...'`) and lives only in .env.
--
-- Types are the narrowest thing that cannot overflow: counts are smallint, confidence
-- is numeric(3,2) because it is always 0.00-1.00, timestamps are timestamptz because
-- gitscout stores UTC ISO-8601 throughout. Nothing is text where a real type exists.

create schema if not exists gitscout;
revoke all on schema gitscout from anon, authenticated;

-- ---------------------------------------------------------------------- people

create table if not exists gitscout.people (
    login                 text primary key,
    name                  text,
    company               text,
    location              text,          -- free text, as typed on the profile
    bio                   text,
    website               text,
    twitter               text,
    hireable              boolean,
    followers             integer,
    lead_score            numeric(5,2),  -- 0-100, see scoring.py
    account_type          text,
    github_joined_at      timestamptz,

    -- best address, denormalised here so outreach needs no join
    best_email            text,
    email_source          text,          -- gql_profile | gql_commit | gql_contrib | ...
    email_confidence      numeric(3,2),

    repos_touched         smallint     not null default 0,
    signals               text[]        not null default '{}',  -- forks, issues, prs, ...
    first_seen_at         timestamptz,
    last_seen_at          timestamptz,

    -- what they did, per section
    issues_opened         smallint     not null default 0,
    issues_commented      smallint     not null default 0,
    prs_opened            smallint     not null default 0,
    prs_commented         smallint     not null default 0,
    prs_reviewed          smallint     not null default 0,
    discussions_opened    smallint     not null default 0,
    discussions_commented smallint     not null default 0,
    discussions_answered  smallint     not null default 0,

    -- GitHub has no timezone field and normalises every timestamp to UTC, so this is
    -- an ESTIMATE from when they are active: the hour histogram below, shifted so
    -- their busiest hours sit in a normal working day.
    --
    -- Measured against 173 people here whose `location` names an unambiguous country:
    -- median error 2 hours, 71% within 3 hours, 62% in the right broad region. Good
    -- enough to pick a sending window, not good enough to quote for one person --
    -- prefer `location` wherever it is filled in.
    estimated_utc_offset  smallint,
    activity_hours_utc    jsonb,         -- {"0": 3, "9": 41, ...} UTC hour -> events

    github_fetched_at     timestamptz,   -- when gitscout last refreshed the profile
    synced_at             timestamptz  not null default now(),

    -- from the person's own profile (socialAccounts, or a LinkedIn link in the
    -- website field), canonicalised to https://www.linkedin.com/in/<slug>
    linkedin_url          text,
    public_repos_count    integer,
    last_enriched_at      timestamptz
);

-- Most queries are "contactable leads, best first", and ~40% of rows have no address,
-- so a partial index keeps it small.
create index if not exists people_email_idx
    on gitscout.people (best_email) where best_email is not null;
create index if not exists people_score_idx
    on gitscout.people (lead_score desc nulls last);
create index if not exists people_company_idx
    on gitscout.people (company) where company is not null;
create index if not exists people_linkedin_idx
    on gitscout.people (linkedin_url) where linkedin_url is not null;
-- GIN so `where signals @> '{contribs}'` is an index scan.
create index if not exists people_signals_idx on gitscout.people using gin (signals);

create table if not exists gitscout.person_emails (
    login       text        not null references gitscout.people (login) on delete cascade,
    email       text        not null,
    source      text        not null,
    confidence  numeric(3,2),
    found_at    timestamptz,
    synced_at   timestamptz not null default now(),
    primary key (login, email, source)
);

create index if not exists person_emails_email_idx on gitscout.person_emails (email);

-- ---------------------------------------------------------------- repositories
--
-- Every repository-scoped row carries `repo` = 'owner/name'. That name changes when
-- a repository is renamed or transferred; GitHub's databaseId does not. The registry
-- holds the stable id once, and everything joins to it by name.

create table if not exists gitscout.repositories (
    repo               text primary key,          -- owner/name
    repository_id      bigint unique,             -- GitHub databaseId, stable
    node_id            text,
    owner              text,
    name               text,
    url                text,
    description        text,
    homepage           text,
    stars              integer,
    forks              integer,
    language           text,
    topics             text[]      not null default '{}',
    is_archived        boolean,
    github_fetched_at  timestamptz,
    synced_at          timestamptz not null default now()
);

create table if not exists gitscout.sync_runs (
    id              serial primary key,
    mode            text        not null,       -- full | incremental | refresh
    since           timestamptz,
    people          integer     not null default 0,
    person_emails   integer     not null default 0,
    threads         integer     not null default 0,
    comments        integer     not null default 0,
    seconds         numeric(8,2),
    status          text        not null default 'ok',
    error           text,
    started_at      timestamptz not null default now()
);

-- ------------------------------------------------- one schema per repository
--
-- Trivy's schema is created here because gitscout.ensure_repo_schema() clones it
-- (CREATE TABLE ... LIKE ... INCLUDING ALL) for every repository that follows. The
-- name is gitscout.repo_schema_name(): "Defendermate-Github1-<Name>" (Trivy for trivy),
-- non-alphanumerics folded to '-'. Upper-case and hyphens: always double-quoted.

create schema if not exists "Defendermate-Github1-Trivy";
revoke all on schema "Defendermate-Github1-Trivy" from anon, authenticated;

create table if not exists "Defendermate-Github1-Trivy".threads (
    repo               text        not null,
    kind               text        not null
                                   constraint threads_kind_check
                                   check (kind in ('issues', 'prs', 'discussions')),
    number             integer     not null,
    url                text,
    title              text,
    body               text,                    -- the full original post
    state              text,                    -- OPEN | CLOSED | MERGED
    state_reason       text,                    -- COMPLETED | NOT_PLANNED | ...
    closed             boolean,
    author             text,
    author_type        text,
    author_association text,                    -- FIRST_TIME_CONTRIBUTOR | MEMBER | ...
    created_at         timestamptz,
    updated_at         timestamptz,
    closed_at          timestamptz,
    locked             boolean,
    comment_count      integer,                 -- what GitHub reports
    reaction_count     integer,
    labels             text[]      not null default '{}',
    assignees          text[]      not null default '{}',   -- issues and PRs only
    milestone          text,
    participants       text[]      not null default '{}',
    node_id            text,
    comments_stored    smallint    not null default 0,       -- what we actually hold
    comments_truncated boolean     not null default false,
    source_deleted_at  timestamptz,                 -- set by the weekly deletion sweep; never deleted   -- true only while a gap remains

    -- pull requests
    merged             boolean,
    merged_at          timestamptz,
    merged_by          text,
    is_draft           boolean,
    additions          integer,
    deletions          integer,
    changed_files      integer,
    commits            integer,
    base_ref           text,
    head_ref           text,
    review_decision    text,

    -- discussions
    category           text,                    -- Q&A, Bugs, Ideas, ...
    category_emoji     text,
    is_answerable      boolean,                 -- only Q&A-style categories are
    upvotes            integer,
    is_answered        boolean,
    answer_author      text,
    answer_at          timestamptz,

    github_fetched_at  timestamptz,
    synced_at          timestamptz not null default now(),
    primary key (repo, kind, number)
);

create index if not exists threads_author_idx
    on "Defendermate-Github1-Trivy".threads (author) where author is not null;
create index if not exists threads_kind_created_idx
    on "Defendermate-Github1-Trivy".threads (kind, created_at desc);
create index if not exists threads_labels_idx
    on "Defendermate-Github1-Trivy".threads using gin (labels);
create index if not exists threads_category_idx
    on "Defendermate-Github1-Trivy".threads (category) where category is not null;

create table if not exists "Defendermate-Github1-Trivy".comments (
    repo               text        not null,
    kind               text        not null,
    number             integer     not null,
    comment_id         text        not null,
    comment_type       text        not null default 'comment'
                                   constraint comments_comment_type_check
                                   check (comment_type in ('comment', 'review', 'reply', 'review_comment')),
    author             text,
    author_type        text,
    author_association text,
    body               text,                    -- the full comment
    created_at         timestamptz,
    updated_at         timestamptz,
    position           smallint    not null default 0,   -- order within its own type
    is_answer          boolean,                 -- discussions: this one solved it
    upvotes            integer,
    review_state       text,                    -- APPROVED | CHANGES_REQUESTED | ...
    parent_id          text,                    -- replies: the comment; review_comment: the review
    synced_at          timestamptz not null default now(),
    url                text,                    -- the comment's own GitHub link
    file_path          text,                    -- review_comment only: the file it is on
    primary key (repo, kind, number, comment_id),
    foreign key (repo, kind, number)
        references "Defendermate-Github1-Trivy".threads (repo, kind, number) on delete cascade
);

comment on column "Defendermate-Github1-Trivy".comments.comment_type is
    'comment = ordinary thread comment; review = a PR review verdict (body may be '
    'empty); reply = a discussion reply (parent_id = the comment); review_comment = '
    'an inline code comment on a PR (parent_id = the review).';
comment on column "Defendermate-Github1-Trivy".comments.url is
    'The comment''s own GitHub link, as returned by the API.';
comment on column "Defendermate-Github1-Trivy".comments.file_path is
    'review_comment only: the file the review thread is on.';

create index if not exists comments_author_idx
    on "Defendermate-Github1-Trivy".comments (author) where author is not null;
-- "who answered someone's question" is the highest-signal query in the whole mirror.
create index if not exists comments_answer_idx
    on "Defendermate-Github1-Trivy".comments (author) where is_answer;

-- One row per person per repo per kind (forks, issues, prs, discussions, contribs,
-- stars). Threads cover issues/PRs/discussions in depth; this is what carries forks
-- and commits, which have no thread.
create table if not exists "Defendermate-Github1-Trivy".interactions (
    repo               text        not null,
    login              text        not null,
    kind               text        not null,
    user_type          text,
    occurred_at        timestamptz,
    extra              text,                      -- fork full name, commit oid, url
    first_recorded_at  timestamptz,
    synced_at          timestamptz not null default now(),
    primary key (repo, login, kind)
);

create index if not exists interactions_login_idx
    on "Defendermate-Github1-Trivy".interactions (login);

-- ------------------------------------------------------ least-privilege writer
--
-- Created without a password and unable to log in. The password is set once, out of
-- band, so it never appears in a file in this repository:
--     alter role gitscout_writer login password '<generated>';

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'gitscout_writer') then
        create role gitscout_writer nologin;
    end if;
end
$$;

grant usage on schema gitscout, "Defendermate-Github1-Trivy" to gitscout_writer;
grant select, insert, update on
    gitscout.repositories, gitscout.people, gitscout.person_emails,
    "Defendermate-Github1-Trivy".threads, "Defendermate-Github1-Trivy".comments,
    "Defendermate-Github1-Trivy".interactions
    to gitscout_writer;
grant select, insert on gitscout.sync_runs to gitscout_writer;
grant usage, select on sequence gitscout.sync_runs_id_seq to gitscout_writer;

-- RLS on with zero policies blocks every non-bypass role, anon and authenticated
-- included. The writer gets exactly the access above through one policy per table,
-- and no one else gets anything.
do $$
declare
    t text;
begin
    foreach t in array array[
        'gitscout.repositories', 'gitscout.people', 'gitscout.person_emails',
        'gitscout.sync_runs',
        '"Defendermate-Github1-Trivy".threads', '"Defendermate-Github1-Trivy".comments',
        '"Defendermate-Github1-Trivy".interactions']
    loop
        execute format('alter table %s enable row level security', t);
        if not exists (select 1 from pg_policy
                        where polrelid = t::regclass and polname = 'gitscout_writer_rw') then
            execute format(
                'create policy gitscout_writer_rw on %s for all '
                'to gitscout_writer using (true) with check (true)', t);
        end if;
    end loop;
end
$$;

-- ------------------------------------------- every repository, through one view
--
-- gitscout.threads_all / comments_all / interactions_all are UNION ALL over every
-- "Defendermate-Github1-*" schema. The named views below read those, so adding a
-- repository never touches a view by hand: gitscout.ensure_repo_schema() creates the
-- schema and rebuilds the unions, and the push calls it for each registered repo.

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

revoke execute on function gitscout.repo_schema_name(text) from public, anon, authenticated;
revoke execute on function gitscout.ensure_repo_schema(text) from public, anon, authenticated;
grant execute on function gitscout.ensure_repo_schema(text) to gitscout_writer;

-- ------------------------------------------------- reading it for many people
--
-- The question this mirror exists to answer is "show me these people and what they
-- actually said". Doing that by hand means joining four tables and remembering which
-- side the emails hang off, so it is wrapped up here instead.

-- Many people at once:
--     select * from gitscout.details_for(array['alice','bob'], array['issues']);
-- `sections` null means every section.
create or replace function gitscout.details_for(
    logins   text[],
    sections text[] default null
)
returns setof gitscout.thread_detail
language sql
stable
as $$
    select *
    from gitscout.thread_detail d
    where d.login = any(logins)
      and (sections is null or d.section = any(sections))
    order by d.login, d.created_at desc;
$$;

-- Everyone who touched one thread, with how to reach them:
--     select * from gitscout.people_in('aquasecurity/trivy', 'issues', 1234);
create or replace function gitscout.people_in(
    repo_name text,
    section   text,
    thread_no integer
)
returns table (
    login text, name text, best_email text, role text,
    comments bigint, estimated_utc_offset smallint, company text, location text
)
language sql
stable
as $$
    select v.login, p.name, p.best_email, v.role,
           (select count(*) from gitscout.comments_all c
             where c.repo = v.repo and c.kind = v.kind
               and c.number = v.number and c.author = v.login),
           p.estimated_utc_offset, p.company, p.location
    from gitscout.participation v
    left join gitscout.people p on p.login = v.login
    where v.repo = repo_name and v.kind = section and v.number = thread_no
    order by v.role, v.login;
$$;

revoke execute on function gitscout.details_for(text[], text[]) from public, anon, authenticated;
revoke execute on function gitscout.people_in(text, text, integer) from public, anon, authenticated;

-- ================================================================ platform foundation
--
-- Repository configuration, the durable job queue, the application's own tables and
-- the dashboard role gitscout_app (migration 0011). The worker and the dashboard read
-- and write these; mirrored data is never touched by them. gitscout_app is created
-- NOLOGIN; its password is set out of band like the writer's:
--     alter role gitscout_app login password '<generated>';

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


-- --------------------------------------------------- the final outreach dataset
--
-- Lives in "Defendermate2", quoted on purpose: it matches the existing "Defendermate"
-- schema's casing, and an unquoted name would fold to lower case.
--
-- Materialized because a plain view was measured at 69 s for a count (every row
-- rebuilt its JSON detail on each read); stored, the same count is under 2 s. It is
-- refreshed CONCURRENTLY after each push, so readers keep the previous result for the
-- whole refresh and a failed refresh leaves the last good dataset serving.
--
-- Materialized views cannot carry RLS; access is by grants alone, and nothing is
-- granted to anon or authenticated.

create schema if not exists "Defendermate2";
revoke all on schema "Defendermate2" from anon, authenticated;

create materialized view if not exists "Defendermate2".final_outreach_users
as
with
-- every (repo, person, thread) they took part in, with the thread's facts
involvement as (
    -- Built from the base tables. It used to read gitscout.participation and join
    -- that GROUP BY result back to threads on (repo, kind, number); the planner then
    -- estimated ONE row, chose a nested loop over 9,153 comment groups, and a refresh
    -- took 143 s. From the base tables it takes about 3 s, same results.
    select t.repo, t.kind, t.number, t.author as login, 'author'::text as role,
           t.url, t.title, t.state, t.closed, t.labels, t.category, t.upvotes,
           t.is_answered, t.merged, t.created_at, t.updated_at, t.author,
           t.comment_count, t.additions, t.deletions,
           t.body as description
    from gitscout.threads_all t
    where t.author is not null
      and t.source_deleted_at is null
      and not exists (select 1 from gitscout.exclusions e
                       where e.repo = t.repo and e.kind = 'thread'
                         and e.key = t.kind || '/' || t.number and e.removed_at is null)
    union all
    -- everyone else who wrote in the thread; 'commenter' < 'reviewer', so someone
    -- who did both reads as commenter, the same rule as gitscout.participation
    select t.repo, t.kind, t.number, c.login, c.role,
           t.url, t.title, t.state, t.closed, t.labels, t.category, t.upvotes,
           t.is_answered, t.merged, t.created_at, t.updated_at, t.author,
           t.comment_count, t.additions, t.deletions,
           t.body as description
    from (select repo, kind, number, author as login,
                 min(case when comment_type = 'review' then 'reviewer'
                          else 'commenter' end) as role
          from gitscout.comments_all
          where author is not null
          group by repo, kind, number, author) c
    join gitscout.threads_all t
      on t.repo = c.repo and t.kind = c.kind and t.number = c.number
    where c.login is distinct from t.author
      and t.source_deleted_at is null
      and not exists (select 1 from gitscout.exclusions e
                       where e.repo = t.repo and e.kind = 'thread'
                         and e.key = t.kind || '/' || t.number and e.removed_at is null)
),
-- what each person wrote in each thread: counts, and their own messages as JSON.
-- `json`, not `jsonb`, on purpose: jsonb stores keys shortest-first; json keeps the
-- order they are built in, which is the order the outreach team reads them in.
own as (
    select c.repo, c.kind, c.number, c.author as login,
           count(*) as own_comments,
           bool_or(coalesce(c.is_answer, false)) as wrote_answer,
           min(c.created_at) as first_comment_at,
           max(c.created_at) as last_comment_at,
           json_agg(json_build_object(
               'type',         c.comment_type,
               'author',       c.author,
               'created_at',   c.created_at,
               'url',          c.url,
               'body',         c.body,
               'is_answer',    c.is_answer,
               'review_state', c.review_state,
               'file_path',    c.file_path,
               'reply_to',     p.author,
               'parent_id',    c.parent_id)
               order by c.created_at, c.comment_type, c.position) as comments
    from gitscout.comments_all c
    -- a discussion reply's parent is another person's (or their own) comment; an
    -- inline code comment's parent is the person's own review, so reply_to is set
    -- for replies only
    left join gitscout.comments_all p
      on p.repo = c.repo and p.kind = c.kind and p.number = c.number
     and p.comment_id = c.parent_id and c.comment_type = 'reply'
    where c.author is not null
    group by c.repo, c.kind, c.number, c.author
),
per_thread as (
    select i.*, coalesce(o.own_comments, 0) as own_comments,
           coalesce(o.wrote_answer, false) as wrote_answer,
           o.first_comment_at, o.last_comment_at,
           coalesce(o.comments, '[]'::json) as comments
    from involvement i
    left join own o
      on o.repo = i.repo and o.kind = i.kind
     and o.number = i.number and o.login = i.login
),
threads_by_person as (
    select repo, login,
           count(*) filter (where kind = 'issues')      as issues_count,
           count(*) filter (where kind = 'discussions') as discussions_count,
           count(*) filter (where kind = 'prs')         as pull_requests_count,
           count(*) filter (where kind = 'issues' and role = 'author')      as issues_opened,
           count(*) filter (where kind = 'discussions' and role = 'author') as discussions_opened,
           count(*) filter (where kind = 'prs' and role = 'author')         as prs_opened,
           count(*) filter (where kind = 'prs' and role = 'reviewer')       as prs_reviewed,
           count(*) filter (where wrote_answer)                             as discussions_answered,
           json_agg(json_build_object(
               'number', number, 'url', url, 'title', title,
               'description', description,
               'state', state, 'labels', labels,
               'created_at', created_at, 'updated_at', updated_at,
               'author', author, 'role', role, 'own_comments', own_comments,
               'comments_count', comment_count, 'comments', comments)
               order by created_at desc) filter (where kind = 'issues')      as issue_details,
           json_agg(json_build_object(
               'number', number, 'url', url, 'title', title,
               'description', description,
               'category', category, 'closed', closed, 'is_answered', is_answered,
               'upvotes', upvotes, 'created_at', created_at, 'updated_at', updated_at,
               'author', author, 'role', role, 'wrote_answer', wrote_answer,
               'own_comments', own_comments,
               'comments_count', comment_count, 'comments', comments)
               order by created_at desc) filter (where kind = 'discussions') as discussion_details,
           json_agg(json_build_object(
               'number', number, 'url', url, 'title', title,
               'description', description,
               'state', state, 'merged', merged, 'additions', additions,
               'deletions', deletions, 'created_at', created_at, 'updated_at', updated_at,
               'author', author, 'role', role, 'own_comments', own_comments,
               'comments_count', comment_count, 'comments', comments)
               order by created_at desc) filter (where kind = 'prs')         as pull_request_details,
           array_agg(distinct repo || '/' || kind || '/' || number)          as thread_ids,
           min(least(created_at, first_comment_at))                          as first_thread_at,
           max(greatest(created_at, last_comment_at))                        as last_thread_at
    from per_thread
    group by repo, login
),
signals_by_person as (
    select repo, login,
           array_agg(distinct kind)                                          as kinds,
           count(*) filter (where kind = 'forks')                            as forks_count,
           jsonb_agg(jsonb_build_object('fork', extra, 'forked_at', occurred_at))
               filter (where kind = 'forks')                                 as fork_details,
           array_agg(distinct repo || '/' || kind)                           as signal_ids,
           min(occurred_at)                                                  as first_signal_at,
           max(occurred_at)                                                  as last_signal_at
    from gitscout.interactions_all
    group by repo, login
),
-- everyone with any presence in a repository, from either source
roster as (
    select repo, login from threads_by_person
    union
    select repo, login from signals_by_person
)
select
    r.repository_id,
    r.owner                                    as repository_owner,
    r.name                                     as repository_name,
    r.url                                      as repository_url,
    'github'                                   as platform,

    p.login                                    as username,
    -- Split only a plain two-or-more-word display name. A single word, or anything
    -- with a comma or digits, is left whole in full_name rather than guessed at.
    case when p.name ~ '^[^\s,0-9]+(\s+[^\s,0-9]+)+$'
         then split_part(p.name, ' ', 1) end   as first_name,
    case when p.name ~ '^[^\s,0-9]+(\s+[^\s,0-9]+)+$'
         then substr(p.name, length(split_part(p.name, ' ', 1)) + 2) end as last_name,
    p.name                                     as full_name,
    'https://github.com/' || p.login           as profile_url,

    p.best_email                               as email,
    p.email_source,
    -- No deliverability check has been run. NULL means "not verified", never "valid".
    null::boolean                              as email_verified,
    p.linkedin_url,

    nullif(btrim(p.company, ' @'), '')         as company_name,
    null::text                                 as company_website,
    -- From the email only when it is a work address; free-mail domains say nothing
    -- about an employer.
    case when p.best_email is not null
          and split_part(p.best_email, '@', 2) !~*
              '^(gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|icloud|me|mac|aol|protonmail|proton|pm|qq|163|126|foxmail|gmx|web|mail|yandex|zoho|hey|fastmail|tutanota|naver|daum)\.'
         then lower(split_part(p.best_email, '@', 2)) end as company_domain,

    p.location,
    p.estimated_utc_offset,
    p.bio,
    p.followers                                as followers_count,
    p.public_repos_count                       as public_repositories_count,

    -- Counts are THIS person's involvement in THIS repository. Repository-wide totals
    -- are the repository_* columns, never these.
    coalesce(t.issues_count, 0)                as issues_count,
    coalesce(t.issues_opened, 0)               as issues_opened,
    t.issue_details,
    coalesce(t.discussions_count, 0)           as discussions_count,
    coalesce(t.discussions_answered, 0)        as discussions_answered,
    t.discussion_details,
    coalesce(t.pull_requests_count, 0)         as pull_requests_count,
    coalesce(t.prs_opened, 0)                  as pull_requests_opened,
    coalesce(t.prs_reviewed, 0)                as pull_requests_reviewed,
    t.pull_request_details,
    coalesce(s.forks_count, 0)                 as forks_count,
    s.fork_details,
    r.stars                                    as repository_stars_count,
    r.forks                                    as repository_forks_count,

    array(select distinct x from unnest(
        coalesce(s.kinds, '{}')
        || case when coalesce(t.discussions_answered, 0) > 0 then array['answered_discussion'] else '{}' end
        || case when coalesce(t.prs_reviewed, 0) > 0 then array['reviewed_pr'] else '{}' end
    ) as x order by x)                          as engagement_signals,


    coalesce(t.thread_ids, '{}') || coalesce(s.signal_ids, '{}') as source_record_ids,

    least(t.first_thread_at, s.first_signal_at)      as first_seen_at,
    greatest(t.last_thread_at, s.last_signal_at)     as last_seen_at,
    greatest(p.synced_at, p.github_fetched_at)       as last_updated_at,
    p.last_enriched_at

from roster x
join gitscout.repositories r on r.repo = x.repo
join gitscout.people p       on p.login = x.login
left join threads_by_person t on t.repo = x.repo and t.login = x.login
left join signals_by_person s on s.repo = x.repo and s.login = x.login
-- people only: bots and organisations are signals about a repo, never prospects
where coalesce(p.account_type, 'User') = 'User'
  and p.login !~* '(\[bot\]|-bot|_bot)$'
  -- a person an administrator excluded stays out, whatever the next sync brings
  and not exists (select 1 from gitscout.exclusions e
                   where e.repo = x.repo and e.kind = 'person'
                     and e.key = p.login and e.removed_at is null);

create unique index if not exists final_outreach_users_key
    on "Defendermate2".final_outreach_users (repository_id, username);
create index if not exists final_outreach_users_repo
    on "Defendermate2".final_outreach_users (repository_owner, repository_name);
create index if not exists final_outreach_users_email
    on "Defendermate2".final_outreach_users (email) where email is not null;
revoke all on "Defendermate2".final_outreach_users from anon, authenticated;

-- SECURITY DEFINER: a materialized view can only be refreshed by its owner, and the
-- scheduled writer must not own anything. search_path is pinned so the function
-- cannot be redirected to an attacker's objects.
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
    -- first time in the dataset = collected now; existing rows keep their date (0014)
    insert into gitscout.dataset_first_collected (repo, login)
    select repository_owner || '/' || repository_name, username
    from "Defendermate2".final_outreach_users
    on conflict (repo, login) do nothing;
    insert into gitscout.sync_runs (mode, people, seconds, status)
    values ('refresh', total, extract(epoch from clock_timestamp() - started), 'ok');
    return total;
end;
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

revoke execute on function gitscout.refresh_outreach() from public, anon, authenticated;
revoke execute on function gitscout.ensure_outreach_view(text) from public, anon, authenticated;

grant execute on function gitscout.refresh_outreach() to gitscout_writer;
grant execute on function gitscout.ensure_outreach_view(text) to gitscout_writer;

-- Future functions created in these schemas start closed instead of open (Postgres's
-- default grants EXECUTE to PUBLIC on every new function).
alter default privileges in schema gitscout revoke execute on functions from public;
alter default privileges in schema "Defendermate2" revoke execute on functions from public;

-- The per-repository views ("Defendermate2"."Defendermate-Github1-<Name>-Final") are not
-- created here: the push creates one for each registered repository through
-- gitscout.ensure_outreach_view(), which needs the registry row to name it.

-- ------------------------------------------------- 4. role gitscout_app (dashboard)
--
-- Created NOLOGIN; the password is set out of band like the writer's:
--     alter role gitscout_app login password '<generated>';

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
grant select on "Defendermate2".final_outreach_users to gitscout_app;  -- per-repo views: ensure_outreach_view()
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

-- ============================================================ observability (0013)
-- alerts, worker liveness, run_history and repo_health views. See migrations/0013.

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

-- ================================================================ exports (0014)
-- first-collected dates, export retention, the private Storage bucket. See migrations/0014.

-- ---------------------------------------------------- 1. first collected dates
create table if not exists gitscout.dataset_first_collected (
    repo          text        not null,
    login         text        not null,
    collected_at  timestamptz not null default now(),
    primary key (repo, login)
);
create index if not exists dataset_first_collected_at_idx
    on gitscout.dataset_first_collected (repo, collected_at);

-- export retention
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

-- ============================================================== dashboard (0015)
-- membership revocation, per-user repository limit, repo_stats. See migrations/0015.

alter table gitscout.memberships add column if not exists revoked_at timestamptz;

insert into gitscout.app_settings (key, value) values ('max_repos_per_user', '10'::jsonb)
on conflict (key) do nothing;

create or replace view gitscout.repo_stats with (security_invoker = true) as
select d.repository_owner || '/' || d.repository_name   as repo,
       count(*)                                          as records,
       count(*) filter (where d.email is not null)        as with_email,
       count(*) filter (where d.linkedin_url is not null) as with_linkedin,
       max(fc.collected_at)                              as newest_collected_at
from "Defendermate2".final_outreach_users d
left join gitscout.dataset_first_collected fc
  on fc.repo = d.repository_owner || '/' || d.repository_name and fc.login = d.username
group by 1;

grant select on gitscout.repo_stats to gitscout_app, gitscout_writer;
revoke all on gitscout.repo_stats from anon, authenticated;
