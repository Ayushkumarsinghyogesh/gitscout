-- Rollback for 0008a/0008b. Moves trivy's tables back and restores the v2 dataset.
begin;
drop view if exists "Defendermate2".final_outreach_users_trivy;
drop materialized view if exists "Defendermate2".final_outreach_users;
drop function if exists gitscout.ensure_repo_schema(text);
drop function if exists gitscout.repo_schema_name(text);
drop function if exists gitscout.rebuild_union_views();
alter table defendermate_trivy_gitscout.threads      set schema gitscout;
alter table defendermate_trivy_gitscout.comments     set schema gitscout;
alter table defendermate_trivy_gitscout.interactions set schema gitscout;
-- the ten gitscout views still name the union views: re-run the view section of
-- src/gitscout/supabase_schema.sql, then drop the unions and the empty schema
drop view if exists gitscout.threads_all, gitscout.comments_all, gitscout.interactions_all;
drop schema if exists defendermate_trivy_gitscout restrict;
-- then re-run section 1 (the dataset) of 0005_fast_refresh_and_writer.sql
commit;
