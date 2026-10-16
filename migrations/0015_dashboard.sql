-- 0015_dashboard.sql  (Phase 8: what the dashboard needs beyond 0011-0014)
--
-- Nothing here changes a row of mirrored data.
--
--   1. memberships.revoked_at: taking a repository away from a user is an update the
--      dashboard role may make (it deletes nothing); re-granting clears it.
--   2. app_settings max_repos_per_user (10): how many repositories a client or admin
--      client may add. The super admin is not limited.
--   3. gitscout.repo_stats (security_invoker): per repository, how many outreach
--      records there are, with an email, with LinkedIn, and when the newest arrived.
--
-- Roles, as the dashboard names them: platform_admin = Super admin, client_admin =
-- Admin client, client_viewer = Client.
--
-- Requires 0014. Rollback: 0015_dashboard.down.sql

begin;

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

commit;
