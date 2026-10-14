-- Rollback for 0003.
select cron.unschedule(jobid) from cron.job where jobname = 'gitscout-outreach-refresh-once';
drop function if exists gitscout.refresh_outreach_once();
