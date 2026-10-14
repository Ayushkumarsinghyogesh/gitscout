-- gitscout -> Supabase mirror
--
-- Run once in the Supabase SQL editor (`gitscout supabase schema -o schema.sql`).
-- Idempotent: re-running adds what is missing and changes nothing else.
--
-- Everything lives in its own `gitscout` schema, NOT in `public`. The host project's
-- `public` schema is described by its own migration history, and dropping unrelated
-- tables into it makes the repo and the database disagree. A separate schema also
-- means permissions can be granted or revoked in one statement.
--
--   gitscout.people          one row per GitHub user
--   gitscout.person_emails   every address found for them, with provenance
--   gitscout.threads         issues, pull requests and discussions
--   gitscout.comments        comments, reviews and replies on those threads
--   gitscout.sync_runs       append-only log of each push
--
-- plus views that are the way you actually read it:
--
--   gitscout.issues          \ the three sections, split out
--   gitscout.pull_requests   /  so you never write "where kind = ..."
--   gitscout.discussions    /
--   gitscout.participation   who was involved in what, and how
--   gitscout.engagement      the big one: person + their email + the thread + its labels
--   gitscout.contactable     people with an email, best lead first
--
-- Types are the narrowest thing that cannot overflow: counts are smallint, confidence
-- is numeric(3,2) because it is always 0.00-1.00, timestamps are timestamptz because
-- gitscout stores UTC ISO-8601 throughout. Nothing is text where a real type exists.

create schema if not exists gitscout;

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
    synced_at             timestamptz  not null default now()
);

-- Most queries are "contactable leads, best first", and ~40% of rows have no address,
-- so a partial index keeps it small.
create index if not exists people_email_idx
    on gitscout.people (best_email) where best_email is not null;
create index if not exists people_score_idx
    on gitscout.people (lead_score desc nulls last);
create index if not exists people_company_idx
    on gitscout.people (company) where company is not null;
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

-- --------------------------------------------------------------------- threads

create table if not exists gitscout.threads (
    repo               text        not null,
    kind               text        not null check (kind in ('issues', 'prs', 'discussions')),
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
    comments_stored    smallint    not null default 0,       -- what we actually fetched
    comments_truncated boolean     not null default false,

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
    on gitscout.threads (author) where author is not null;
create index if not exists threads_kind_created_idx
    on gitscout.threads (kind, created_at desc);
create index if not exists threads_labels_idx on gitscout.threads using gin (labels);
create index if not exists threads_category_idx
    on gitscout.threads (category) where category is not null;

create table if not exists gitscout.comments (
    repo               text        not null,
    kind               text        not null,
    number             integer     not null,
    comment_id         text        not null,
    comment_type       text        not null default 'comment'
                                   check (comment_type in ('comment', 'review', 'reply')),
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
    parent_id          text,                    -- replies: the comment they answer
    synced_at          timestamptz not null default now(),
    primary key (repo, kind, number, comment_id),
    foreign key (repo, kind, number)
        references gitscout.threads (repo, kind, number) on delete cascade
);

create index if not exists comments_author_idx
    on gitscout.comments (author) where author is not null;
-- "who answered someone's question" is the highest-signal query in the whole mirror.
create index if not exists comments_answer_idx
    on gitscout.comments (author) where is_answer;

create table if not exists gitscout.sync_runs (
    id              serial primary key,
    mode            text        not null,       -- full | incremental
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

-- ----------------------------------------------------------------------- views

create or replace view gitscout.issues as
    select * from gitscout.threads where kind = 'issues';

create or replace view gitscout.pull_requests as
    select * from gitscout.threads where kind = 'prs';

create or replace view gitscout.discussions as
    select * from gitscout.threads where kind = 'discussions';

-- Involved = opened it, commented on it, or reviewed it. One row per person per
-- thread: MIN() resolves someone who did several things to the strongest role, and
-- the names are alphabetical on purpose ('author' < 'commenter' < 'reviewer').
create or replace view gitscout.participation as
select repo, kind, number, login, author_type,
       min(role) as role
from (
    select repo, kind, number, author as login, author_type, 'author' as role
    from gitscout.threads where author is not null
    union all
    select repo, kind, number, author as login, author_type,
           case when comment_type = 'review' then 'reviewer' else 'commenter' end
    from gitscout.comments where author is not null
) p
group by repo, kind, number, login, author_type;

-- The one to start from: a person, how to reach them, and the thread they engaged
-- with -- title, body, labels, category and their own comments, already counted.
create or replace view gitscout.engagement as
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
       (select count(*) from gitscout.comments c
         where c.repo = v.repo and c.kind = v.kind
           and c.number = v.number and c.author = v.login) as own_comments
from gitscout.participation v
join gitscout.threads t
  on t.repo = v.repo and t.kind = v.kind and t.number = v.number
left join gitscout.people p on p.login = v.login;

create or replace view gitscout.contactable as
select login, name, best_email, email_source, email_confidence, lead_score, company,
       location, estimated_utc_offset, followers, signals,
       issues_opened, prs_opened, prs_reviewed, discussions_answered, last_seen_at
from gitscout.people
where best_email is not null
order by lead_score desc nulls last;

-- ------------------------------------------------- reading it for many people
--
-- The question this mirror exists to answer is "show me these people and what they
-- actually said". Doing that by hand means joining four tables and remembering which
-- side the emails hang off, so it is wrapped up here instead.

-- One row per (person, thread) with the entire conversation attached as JSON --
-- every comment carrying its author's email and timezone estimate, so a single row
-- is self-contained and nothing has to be joined back.
create or replace view gitscout.thread_detail as
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
    (select count(*) from gitscout.comments c
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
       from gitscout.comments c
       left join gitscout.people cp on cp.login = c.author
      where c.repo = t.repo and c.kind = t.kind and c.number = t.number)
                                               as comments
from gitscout.participation v
join gitscout.threads t
  on t.repo = v.repo and t.kind = v.kind and t.number = v.number
left join gitscout.people p on p.login = v.login;

-- The three sections, pre-filtered.
create or replace view gitscout.issue_detail as
    select * from gitscout.thread_detail where section = 'issues';

create or replace view gitscout.discussion_detail as
    select * from gitscout.thread_detail where section = 'discussions';

create or replace view gitscout.pr_detail as
    select * from gitscout.thread_detail where section = 'prs';

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
           (select count(*) from gitscout.comments c
             where c.repo = v.repo and c.kind = v.kind
               and c.number = v.number and c.author = v.login),
           p.estimated_utc_offset, p.company, p.location
    from gitscout.participation v
    left join gitscout.people p on p.login = v.login
    where v.repo = repo_name and v.kind = section and v.number = thread_no
    order by v.role, v.login;
$$;

-- ------------------------------------------------------------------------- RLS
--
-- `service_role` bypasses RLS, so gitscout keeps writing with it on. Enabling it with
-- no policy for `anon`/`authenticated` is what keeps this out of a browser: these are
-- real people's addresses and the anon key ships to every client.
alter table gitscout.people         enable row level security;
alter table gitscout.person_emails  enable row level security;
alter table gitscout.threads        enable row level security;
alter table gitscout.comments       enable row level security;
alter table gitscout.sync_runs      enable row level security;
