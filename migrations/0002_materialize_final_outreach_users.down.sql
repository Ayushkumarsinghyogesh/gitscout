-- Rollback for 0002: put back the plain views from 0001.
drop view if exists "Defendermate2".final_outreach_users_trivy;
drop materialized view if exists "Defendermate2".final_outreach_users;
-- then re-run the view section of 0001_final_outreach_users.sql
