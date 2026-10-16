-- 0015_dashboard.down.sql

begin;

drop view if exists gitscout.repo_stats;
delete from gitscout.app_settings where key = 'max_repos_per_user';
alter table gitscout.memberships drop column if exists revoked_at;

commit;
