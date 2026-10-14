
create table if not exists gitscout_leads (
    login                 text primary key,
    name                  text,
    company               text,
    location              text,
    bio                   text,
    blog                  text,
    twitter               text,
    hireable              boolean,
    followers             integer,
    score                 numeric(5,2),
    account_type          text,
    account_created_at    timestamptz,

    -- best address, denormalised onto the lead so outreach needs no join
    email                 text,
    email_source          text,
    email_confidence      numeric(3,2),

    repos_touched         smallint     not null default 0,
    kinds                 text[]       not null default '{}',
    first_seen            timestamptz,
    last_seen             timestamptz,

    issues_authored       smallint     not null default 0,
    issues_commented      smallint     not null default 0,
    prs_authored          smallint     not null default 0,
    prs_commented         smallint     not null default 0,
    prs_reviewed          smallint     not null default 0,
    discussions_authored  smallint     not null default 0,
    discussions_commented smallint     not null default 0,
    discussions_answered  smallint     not null default 0,

    -- when gitscout last refreshed the profile, vs when this row was pushed
    source_fetched_at     timestamptz,
    synced_at             timestamptz  not null default now()
);

-- Partial index: most queries are "contactable leads, best first", and roughly 40% of
-- rows have no address, so keeping them out of the index keeps it small.
create index if not exists gitscout_leads_email_idx
    on gitscout_leads (email) where email is not null;
create index if not exists gitscout_leads_score_idx
    on gitscout_leads (score desc nulls last);
create index if not exists gitscout_leads_company_idx
    on gitscout_leads (company) where company is not null;
-- GIN so `where kinds @> '{contribs}'` is an index scan, not a seq scan.
create index if not exists gitscout_leads_kinds_idx on gitscout_leads using gin (kinds);
create index if not exists gitscout_leads_synced_idx on gitscout_leads (synced_at desc);

create table if not exists gitscout_lead_emails (
    login       text        not null references gitscout_leads (login) on delete cascade,
    email       text        not null,
    source      text        not null,
    confidence  numeric(3,2),
    found_at    timestamptz,
    synced_at   timestamptz not null default now(),
    primary key (login, email, source)
);

create index if not exists gitscout_lead_emails_email_idx on gitscout_lead_emails (email);

create table if not exists gitscout_threads (
    repo               text        not null,
    kind               text        not null check (kind in ('issues', 'prs', 'discussions')),
    number             integer     not null,
    url                text,
    title              text,
    body               text,
    state              text,
    state_reason       text,
    author             text,
    author_type        text,
    author_association text,
    created_at         timestamptz,
    updated_at         timestamptz,
    closed_at          timestamptz,
    locked             boolean,
    comment_count      integer,
    reaction_count     integer,
    labels             text[]      not null default '{}',
    assignees          text[]      not null default '{}',
    milestone          text,
    participants       text[]      not null default '{}',
    node_id            text,
    comments_stored    smallint    not null default 0,
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
    category           text,
    upvotes            integer,
    is_answered        boolean,
    answer_author      text,
    answer_at          timestamptz,

    source_fetched_at  timestamptz,
    synced_at          timestamptz not null default now(),
    primary key (repo, kind, number)
);

create index if not exists gitscout_threads_author_idx
    on gitscout_threads (author) where author is not null;
create index if not exists gitscout_threads_kind_created_idx
    on gitscout_threads (kind, created_at desc);
create index if not exists gitscout_threads_labels_idx on gitscout_threads using gin (labels);

create table if not exists gitscout_thread_comments (
    repo               text        not null,
    kind               text        not null,
    number             integer     not null,
    comment_id         text        not null,
    comment_type       text        not null default 'comment'
                                   check (comment_type in ('comment', 'review', 'reply')),
    author             text,
    author_type        text,
    author_association text,
    body               text,
    created_at         timestamptz,
    updated_at         timestamptz,
    position           smallint    not null default 0,
    is_answer          boolean,
    upvotes            integer,
    review_state       text,
    parent_id          text,
    synced_at          timestamptz not null default now(),
    primary key (repo, kind, number, comment_id),
    foreign key (repo, kind, number)
        references gitscout_threads (repo, kind, number) on delete cascade
);

create index if not exists gitscout_thread_comments_author_idx
    on gitscout_thread_comments (author) where author is not null;
-- Partial: "who answered a question" is the highest-signal query in the whole mirror.
create index if not exists gitscout_thread_comments_answer_idx
    on gitscout_thread_comments (author) where is_answer;

create table if not exists gitscout_sync_runs (
    id                serial primary key,
    mode              text        not null,          -- full | incremental
    since             timestamptz,
    leads             integer     not null default 0,
    lead_emails       integer     not null default 0,
    threads           integer     not null default 0,
    thread_comments   integer     not null default 0,
    seconds           numeric(8,2),
    status            text        not null default 'ok',
    error             text,
    started_at        timestamptz not null default now()
);

-- A convenience view for the common question: who is worth contacting, best first.
create or replace view gitscout_contactable as
select login, name, email, email_source, email_confidence, score, company, location,
       followers, kinds, issues_authored, prs_authored, prs_reviewed,
       discussions_answered, last_seen
from gitscout_leads
where email is not null
order by score desc nulls last;

-- Row Level Security.
--
-- `service_role` bypasses RLS entirely, so gitscout keeps writing with RLS on. Turning
-- it on with no policy for `anon`/`authenticated` is what keeps this data out of the
-- browser: these are real people's addresses, and the anon key ships to every client.
-- Add a policy below only if a signed-in dashboard genuinely needs to read it.
alter table gitscout_leads            enable row level security;
alter table gitscout_lead_emails      enable row level security;
alter table gitscout_threads          enable row level security;
alter table gitscout_thread_comments  enable row level security;
alter table gitscout_sync_runs        enable row level security;
