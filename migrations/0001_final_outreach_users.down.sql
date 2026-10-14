-- Rollback for 0001_final_outreach_users.sql
--
-- Removes only what 0001 added. Nothing that existed before it is touched: the
-- gitscout people/emails/threads/comments tables and every "Defendermate" object
-- stay exactly as they are.

drop view if exists "Defendermate2".final_outreach_users_trivy;
drop view if exists "Defendermate2".final_outreach_users;
-- Only dropped if empty; it would otherwise hold views added after this migration.
drop schema if exists "Defendermate2" restrict;

drop index if exists gitscout.people_linkedin_idx;
alter table gitscout.people drop column if exists last_enriched_at;
alter table gitscout.people drop column if exists public_repos_count;
alter table gitscout.people drop column if exists linkedin_url;

drop table if exists gitscout.interactions;
drop table if exists gitscout.repositories;
