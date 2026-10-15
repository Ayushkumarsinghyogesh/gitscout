-- 0008c_people_in_reads_union.sql
--
-- 0008a moved the repository tables out of gitscout and repointed the ten views, but
-- missed gitscout.people_in(): a LANGUAGE sql function resolves the names in its body
-- at call time, so after the move every call failed with
--     relation "gitscout.comments" does not exist
-- Found by calling it during validation. details_for() reads thread_detail and was
-- unaffected. Same signature, same result columns; the one subquery now reads the
-- union view, so it covers every repository.
--
-- Rollback: 0008c_people_in_reads_union.down.sql

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

revoke execute on function gitscout.people_in(text, text, integer) from public, anon, authenticated;
