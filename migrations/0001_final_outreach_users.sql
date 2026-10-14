-- 0001_final_outreach_users.sql
--
-- Adds the repository registry, the raw-signal mirror and the final outreach dataset.
-- Strictly additive: no table, view or row that exists today is dropped or rewritten.
-- Rollback: 0001_final_outreach_users.down.sql
--
-- Apply:  gitscout supabase migrate migrations/0001_final_outreach_users.sql
--         (or paste into the Supabase SQL editor)

-- ------------------------------------------------------------ repository registry
--
-- Every record already carries `repo` = 'owner/name'. That name changes when a repo
-- is renamed or transferred; GitHub's databaseId does not. The registry holds the
-- stable id once, and everything joins to it by name.

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

-- ------------------------------------------------------------- raw signal mirror
--
-- One row per person per repo per kind (fork, issue, pr, discussion, contribs, star).
-- Threads cover issues/PRs/discussions in depth; this is what carries forks and
-- commits, which have no thread.

create table if not exists gitscout.interactions (
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

create index if not exists interactions_login_idx on gitscout.interactions (login);

-- ------------------------------------------------------------ people: new fields

alter table gitscout.people add column if not exists linkedin_url       text;
alter table gitscout.people add column if not exists public_repos_count integer;
alter table gitscout.people add column if not exists last_enriched_at   timestamptz;

create index if not exists people_linkedin_idx
    on gitscout.people (linkedin_url) where linkedin_url is not null;

alter table gitscout.repositories enable row level security;
alter table gitscout.interactions enable row level security;

-- ------------------------------------------------------- the final outreach dataset
--
-- Lives in "Defendermate2" as requested. Quoted on purpose: it matches the existing
-- "Defendermate" schema's casing, and an unquoted name would fold to lower case.
--
-- One generic view keyed by (repository, person), and one thin view per repository.
-- Adding a repository means registering it and adding a one-line view -- no new
-- ingestion code and no new tables.
--
-- A plain view, not a table: it is always current with the mirror, there is no second
-- copy to drift, and a failed sync leaves it showing the last good data. Measured
-- before choosing (see docs/outreach-data-model.md); move to a materialized view only
-- if the query time stops being acceptable.

create schema if not exists "Defendermate2";

-- Nothing here is for the browser: these are real people's addresses.
revoke all on schema "Defendermate2" from anon, authenticated;

create or replace view "Defendermate2".final_outreach_users
with (security_invoker = true)   -- obey the caller's privileges, never the owner's
as
with
-- every (repo, person, thread) they took part in, with the thread's facts
involvement as (
    select v.repo, v.login, v.kind, v.number, v.role,
           t.url, t.title, t.state, t.closed, t.labels, t.category, t.upvotes,
           t.is_answered, t.merged, t.created_at, t.updated_at, t.author,
           t.comment_count, t.additions, t.deletions,
           left(t.body, 500) as excerpt
    from gitscout.participation v
    join gitscout.threads t
      on t.repo = v.repo and t.kind = v.kind and t.number = v.number
),
-- what each person wrote in each thread
own as (
    select repo, kind, number, author as login,
           count(*) as own_comments,
           bool_or(coalesce(is_answer, false)) as wrote_answer,
           min(created_at) as first_comment_at,
           max(created_at) as last_comment_at
    from gitscout.comments
    where author is not null
    group by repo, kind, number, author
),
per_thread as (
    select i.*, coalesce(o.own_comments, 0) as own_comments,
           coalesce(o.wrote_answer, false) as wrote_answer,
           o.first_comment_at, o.last_comment_at
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
           jsonb_agg(jsonb_build_object(
               'number', number, 'url', url, 'title', title, 'state', state,
               'labels', labels, 'created_at', created_at, 'updated_at', updated_at,
               'author', author, 'role', role, 'comments_count', comment_count,
               'own_comments', own_comments, 'excerpt', excerpt)
               order by created_at desc) filter (where kind = 'issues')      as issue_details,
           jsonb_agg(jsonb_build_object(
               'number', number, 'url', url, 'title', title, 'category', category,
               'closed', closed, 'is_answered', is_answered, 'upvotes', upvotes,
               'created_at', created_at, 'updated_at', updated_at, 'author', author,
               'role', role, 'comments_count', comment_count,
               'own_comments', own_comments, 'wrote_answer', wrote_answer,
               'excerpt', excerpt)
               order by created_at desc) filter (where kind = 'discussions') as discussion_details,
           jsonb_agg(jsonb_build_object(
               'number', number, 'url', url, 'title', title, 'state', state,
               'merged', merged, 'created_at', created_at, 'updated_at', updated_at,
               'author', author, 'role', role, 'additions', additions,
               'deletions', deletions, 'comments_count', comment_count,
               'own_comments', own_comments, 'excerpt', excerpt)
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
    from gitscout.interactions
    group by repo, login
),
-- everyone with any presence in a repository, from either source
roster as (
    select repo, login from threads_by_person
    union
    select repo, login from signals_by_person
),
-- Distinct, and split by key: a person listed twice, or matching on both email and
-- LinkedIn, must still produce exactly one row.
dnc_email as (
    select distinct lower(btrim("Email")) as email
    from "Defendermate"."Do Not Contact List"
    where nullif(btrim("Email"), '') is not null
),
dnc_linkedin as (
    select distinct lower(btrim("LinkedInUrl")) as linkedin
    from "Defendermate"."Do Not Contact List"
    where nullif(btrim("LinkedInUrl"), '') is not null
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
    p.email_confidence,
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

    p.lead_score                               as relevance_score,
    concat_ws('; ',
        'active in ' || r.repo,
        case when t.issues_opened > 0 then 'opened ' || t.issues_opened || ' issue(s)' end,
        case when t.issues_count > t.issues_opened
             then 'took part in ' || (t.issues_count - t.issues_opened) || ' more issue(s)' end,
        case when t.prs_opened > 0 then 'opened ' || t.prs_opened || ' PR(s)' end,
        case when t.prs_reviewed > 0 then 'reviewed ' || t.prs_reviewed || ' PR(s)' end,
        case when t.discussions_answered > 0
             then 'answered ' || t.discussions_answered || ' discussion(s)' end,
        case when 'contribs' = any(s.kinds) then 'has commits on the default branch' end,
        case when s.forks_count > 0 then 'forked the repository' end,
        case when nullif(btrim(p.company, ' @'), '') is not null
             then 'lists company ' || btrim(p.company, ' @') end
    )                                          as qualification_reason,

    coalesce(t.thread_ids, '{}') || coalesce(s.signal_ids, '{}') as source_record_ids,

    least(t.first_thread_at, s.first_signal_at)      as first_seen_at,
    greatest(t.last_thread_at, s.last_signal_at)     as last_seen_at,
    greatest(p.synced_at, p.github_fetched_at)       as last_updated_at,
    p.last_enriched_at,

    -- Derived, not stored: the real workflow status belongs to the CRM. What this
    -- does guarantee is that nobody on the do-not-contact list reads as contactable.
    case when d1.email is not null or d2.linkedin is not null
         then 'do_not_contact' else 'not_contacted' end as outreach_status,
    case when d1.email is not null or d2.linkedin is not null then 'suppressed'
         when p.best_email is null and p.linkedin_url is null then 'no_contact_channel'
         when p.best_email is not null and p.email_confidence < 0.80 then 'email_low_confidence'
         when p.name is null then 'name_missing'
         else 'contactable' end                as data_quality_status
from roster x
join gitscout.repositories r on r.repo = x.repo
join gitscout.people p       on p.login = x.login
left join threads_by_person t on t.repo = x.repo and t.login = x.login
left join signals_by_person s on s.repo = x.repo and s.login = x.login
left join dnc_email d1    on d1.email = lower(p.best_email)
left join dnc_linkedin d2 on d2.linkedin = lower(p.linkedin_url)
-- people only: bots and organisations are signals about a repo, never prospects
where coalesce(p.account_type, 'User') = 'User'
  and p.login !~* '(\[bot\]|-bot|_bot)$';

-- One per repository. Adding a repository is one more line like this.
create or replace view "Defendermate2".final_outreach_users_trivy
with (security_invoker = true)
as
select * from "Defendermate2".final_outreach_users
where repository_owner = 'aquasecurity' and repository_name = 'trivy';
