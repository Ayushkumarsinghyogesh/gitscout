-- 0003_refresh_function.sql
--
-- Refreshes the outreach dataset from a one-off pg_cron job.
--
-- Why a function: pg_cron runs a job's command as ONE transaction. A command of
-- "unschedule myself; refresh" therefore loses the unschedule if the refresh fails
-- -- observed directly: the job stayed scheduled for the whole refresh and only
-- disappeared on commit. A failed refresh would have retried every minute, forever,
-- on the production database.
--
-- Here the refresh runs in an inner BEGIN/EXCEPTION block, which is a subtransaction:
-- if it fails, only it rolls back. The unschedule always commits, the job always
-- removes itself, and the failure is written to gitscout.sync_runs instead of lost.
--
-- Rollback: 0003_refresh_function.down.sql

create or replace function gitscout.refresh_outreach_once()
returns void
language plpgsql
set search_path = ''
as $$
declare
    started timestamptz := clock_timestamp();
begin
    -- First, and outside the inner block, so it commits whatever happens next.
    perform cron.unschedule('gitscout-outreach-refresh-once');

    begin
        perform set_config('statement_timeout', '0', true);
        refresh materialized view concurrently "Defendermate2".final_outreach_users;

        insert into gitscout.sync_runs (mode, seconds, status)
        values ('refresh', extract(epoch from clock_timestamp() - started), 'ok');
    exception when others then
        insert into gitscout.sync_runs (mode, seconds, status, error)
        values ('refresh', extract(epoch from clock_timestamp() - started), 'failed', sqlerrm);
        raise warning 'gitscout outreach refresh failed: %', sqlerrm;
    end;
end;
$$;

-- Functions are executable by PUBLIC by default. This one is for pg_cron only.
revoke execute on function gitscout.refresh_outreach_once() from public, anon, authenticated;
