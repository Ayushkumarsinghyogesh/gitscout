-- 0006_harden_views_and_functions.sql
--
-- Found in an audit after 0005, none of it exploitable today -- anon and
-- authenticated have no USAGE on the gitscout or "Defendermate2" schemas, and the
-- project's Data API is off -- but each would turn into a leak the moment either of
-- those changed:
--
--   * all ten gitscout views ran with their owner's privileges, so they bypassed the
--     RLS on the tables underneath. RLS-on-the-table is no protection if the view on
--     top of it ignores it.
--   * gitscout.details_for() and gitscout.people_in() were executable by PUBLIC,
--     Postgres's default for every new function.
--
-- After this, every gitscout view obeys the caller's own privileges and RLS, and no
-- gitscout function -- present or future -- is executable by PUBLIC unless granted.
--
-- Rollback: 0006_harden_views_and_functions.down.sql

alter view gitscout.contactable        set (security_invoker = true);
alter view gitscout.discussion_detail  set (security_invoker = true);
alter view gitscout.discussions        set (security_invoker = true);
alter view gitscout.engagement         set (security_invoker = true);
alter view gitscout.issue_detail       set (security_invoker = true);
alter view gitscout.issues             set (security_invoker = true);
alter view gitscout.participation      set (security_invoker = true);
alter view gitscout.pr_detail          set (security_invoker = true);
alter view gitscout.pull_requests      set (security_invoker = true);
alter view gitscout.thread_detail      set (security_invoker = true);

revoke execute on function gitscout.details_for(text[], text[]) from public, anon, authenticated;
revoke execute on function gitscout.people_in(text, text, integer) from public, anon, authenticated;

-- Future functions created in these schemas start closed instead of open.
alter default privileges in schema gitscout revoke execute on functions from public;
alter default privileges in schema "Defendermate2" revoke execute on functions from public;
