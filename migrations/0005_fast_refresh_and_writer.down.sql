-- Rollback for 0005. Restores the 0002/0004 shape; nothing outside gitscout's own
-- objects is touched.
do $$
declare t text;
begin
    foreach t in array array['repositories', 'people', 'person_emails', 'threads',
                             'comments', 'interactions', 'sync_runs']
    loop
        execute format('drop policy if exists gitscout_writer_rw on gitscout.%I', t);
    end loop;
end
$$;
revoke all on all tables in schema gitscout from gitscout_writer;
revoke all on all sequences in schema gitscout from gitscout_writer;
revoke usage on schema gitscout from gitscout_writer;
drop function if exists gitscout.ensure_outreach_view(text);
drop function if exists gitscout.refresh_outreach();
drop role if exists gitscout_writer;
-- then re-run 0002 (dataset) and 0004 (refresh functions)
