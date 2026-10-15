-- 0008c_people_in_reads_union.down.sql
--
-- Restores the pre-0008a body. Only meaningful together with
-- 0008_repo_schemas_and_outreach_v3.down.sql, which moves gitscout.comments back;
-- on its own it leaves the function failing exactly as it did before 0008c.

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

revoke execute on function gitscout.people_in(text, text, integer) from public, anon, authenticated;
