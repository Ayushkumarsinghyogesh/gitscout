-- 0004_refresh_cannot_loop.sql
--
-- Incident fix. 0003's refresh job looped: 5 failed runs, 13:23-13:30 UTC on
-- 2026-10-13, each a two-minute REFRESH attempt against production. No data was
-- lost -- a failed REFRESH ... CONCURRENTLY rolls back and the previous dataset
-- kept being served -- but the job could not stop itself.
--
-- Two causes compounded:
--   1. The server's default statement_timeout (2 min) applies to pg_cron sessions,
--      and set_config() inside the function cannot extend the timer of the
--      top-level statement that is already running.
--   2. PL/pgSQL `WHEN OTHERS` does not catch QUERY_CANCELED (SQLSTATE 57014) --
--      a documented exception. The timeout escaped the handler, the transaction
--      aborted, the unschedule rolled back with it, and pg_cron fired again.
-- 0003 was tested with RAISE EXCEPTION, which WHEN OTHERS does catch, so the test
-- passed without exercising the real failure.
--
-- The fix does not rely on any single guard:
--   * the cron command sets statement_timeout = 0 as a separate statement first;
--   * this function names query_canceled explicitly;
--   * and the job is scheduled for one specific minute (`MI HH DD MM *`), so even
--     if every guard failed, the next firing would be a year away, not a minute.
--
-- Rollback: re-run 0003_refresh_function.sql (not recommended -- it can loop).

create or replace function gitscout.refresh_outreach_once()
returns void
language plpgsql
set search_path = ''
as $$
declare
    started timestamptz := clock_timestamp();
begin
    -- Tolerates the job being absent, so the function is also safe to call by hand.
    perform cron.unschedule(jobid) from cron.job
     where jobname = 'gitscout-outreach-refresh-once';

    begin
        refresh materialized view concurrently "Defendermate2".final_outreach_users;
        insert into gitscout.sync_runs (mode, seconds, status)
        values ('refresh', extract(epoch from clock_timestamp() - started), 'ok');
    exception
        when query_canceled then         -- NOT covered by WHEN OTHERS
            insert into gitscout.sync_runs (mode, seconds, status, error)
            values ('refresh', extract(epoch from clock_timestamp() - started),
                    'failed', 'cancelled: ' || sqlerrm);
            raise warning 'gitscout outreach refresh cancelled: %', sqlerrm;
        when others then
            insert into gitscout.sync_runs (mode, seconds, status, error)
            values ('refresh', extract(epoch from clock_timestamp() - started),
                    'failed', sqlerrm);
            raise warning 'gitscout outreach refresh failed: %', sqlerrm;
    end;
end;
$$;

revoke execute on function gitscout.refresh_outreach_once() from public, anon, authenticated;

-- The one place the job is created, so the loader cannot get the schedule wrong.
-- Pinned to the next whole minute in pg_cron's timezone (GMT here).
create or replace function gitscout.queue_outreach_refresh()
returns text
language sql
set search_path = ''
as $$
    select cron.schedule(
        'gitscout-outreach-refresh-once',
        to_char(now() at time zone current_setting('cron.timezone') + interval '1 minute',
                'FMMI FMHH24 FMDD FMMM') || ' *',
        'set statement_timeout = 0; select gitscout.refresh_outreach_once();'
    )::text;
$$;

revoke execute on function gitscout.queue_outreach_refresh() from public, anon, authenticated;
