-- 0008e_own_comments_details.down.sql
--
-- Restores the 0008d dataset: jsonb detail columns whose `comments` hold the whole
-- conversation of each thread. Rebuilds the materialized view and the trivy view.

begin;

drop view if exists "Defendermate2".final_outreach_users_trivy;
drop materialized view if exists "Defendermate2".final_outreach_users;

create materialized view "Defendermate2".final_outreach_users
as
with
-- the whole conversation of each thread, once, as JSON; each comment carries its
-- author's best email so the row needs no join to name a commenter
conversation as (
    select c.repo, c.kind, c.number,
           jsonb_agg(jsonb_build_object(
               'type',         c.comment_type,
               'author',       c.author,
               'author_email', cp.best_email,
               'created_at',   c.created_at,
               'url',          c.url,
               'body',         c.body,
               'is_answer',    c.is_answer,
               'review_state', c.review_state,
               'file_path',    c.file_path,
               'parent_id',    c.parent_id)
               order by c.created_at, c.comment_type, c.position) as comments
    from gitscout.comments_all c
    left join gitscout.people cp on cp.login = c.author
    group by c.repo, c.kind, c.number
),
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
),
-- what each person wrote in each thread
own as (
    select repo, kind, number, author as login,
           count(*) as own_comments,
           bool_or(coalesce(is_answer, false)) as wrote_answer,
           min(created_at) as first_comment_at,
           max(created_at) as last_comment_at
    from gitscout.comments_all
    where author is not null
    group by repo, kind, number, author
),
per_thread as (
    select i.*, coalesce(o.own_comments, 0) as own_comments,
           coalesce(o.wrote_answer, false) as wrote_answer,
           o.first_comment_at, o.last_comment_at,
           coalesce(cv.comments, '[]'::jsonb) as comments
    from involvement i
    left join own o
      on o.repo = i.repo and o.kind = i.kind
     and o.number = i.number and o.login = i.login
    left join conversation cv
      on cv.repo = i.repo and cv.kind = i.kind and cv.number = i.number
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
               'own_comments', own_comments,
               'description', description, 'comments', comments)
               order by created_at desc) filter (where kind = 'issues')      as issue_details,
           jsonb_agg(jsonb_build_object(
               'number', number, 'url', url, 'title', title, 'category', category,
               'closed', closed, 'is_answered', is_answered, 'upvotes', upvotes,
               'created_at', created_at, 'updated_at', updated_at, 'author', author,
               'role', role, 'comments_count', comment_count,
               'own_comments', own_comments, 'wrote_answer', wrote_answer,
               'description', description, 'comments', comments)
               order by created_at desc) filter (where kind = 'discussions') as discussion_details,
           jsonb_agg(jsonb_build_object(
               'number', number, 'url', url, 'title', title, 'state', state,
               'merged', merged, 'created_at', created_at, 'updated_at', updated_at,
               'author', author, 'role', role, 'additions', additions,
               'deletions', deletions, 'comments_count', comment_count,
               'own_comments', own_comments,
               'description', description, 'comments', comments)
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
  and p.login !~* '(\[bot\]|-bot|_bot)$';

create unique index if not exists final_outreach_users_key
    on "Defendermate2".final_outreach_users (repository_id, username);
create index if not exists final_outreach_users_repo
    on "Defendermate2".final_outreach_users (repository_owner, repository_name);
create index if not exists final_outreach_users_email
    on "Defendermate2".final_outreach_users (email) where email is not null;
revoke all on "Defendermate2".final_outreach_users from anon, authenticated;

select gitscout.ensure_outreach_view('aquasecurity/trivy');

commit;
