-- Rollback for 0006 (re-opens what 0006 closed; not recommended).
alter default privileges in schema "Defendermate2" grant execute on functions to public;
alter default privileges in schema gitscout grant execute on functions to public;
grant execute on function gitscout.people_in(text, text, integer) to public;
grant execute on function gitscout.details_for(text[], text[]) to public;
alter view gitscout.thread_detail      reset (security_invoker);
alter view gitscout.pull_requests      reset (security_invoker);
alter view gitscout.pr_detail          reset (security_invoker);
alter view gitscout.participation      reset (security_invoker);
alter view gitscout.issues             reset (security_invoker);
alter view gitscout.issue_detail       reset (security_invoker);
alter view gitscout.engagement         reset (security_invoker);
alter view gitscout.discussions        reset (security_invoker);
alter view gitscout.discussion_detail  reset (security_invoker);
alter view gitscout.contactable        reset (security_invoker);
