-- 0014_exports.down.sql
--
-- Restores the 0012 refresh_outreach(), drops the first-collected dates and the
-- exports.deleted_at column. The Storage bucket is left in place: deleting it would
-- delete the export files, which is a decision for a person, not a rollback.

begin;

create or replace function gitscout.refresh_outreach()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    started timestamptz := clock_timestamp();
    total   integer;
begin
    refresh materialized view concurrently "Defendermate2".final_outreach_users;
    select count(*) into total from "Defendermate2".final_outreach_users;
    insert into gitscout.sync_runs (mode, people, seconds, status)
    values ('refresh', total, extract(epoch from clock_timestamp() - started), 'ok');
    return total;
end;
$$;

drop table if exists gitscout.dataset_first_collected;
alter table gitscout.exports drop column if exists deleted_at;

commit;
