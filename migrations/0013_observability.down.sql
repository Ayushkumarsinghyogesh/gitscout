-- 0013_observability.down.sql
--
-- Removes the Phase 6 observability objects. Jobs, repositories and mirrored data are
-- untouched; the worker keeps running without alerts (it skips them when the table
-- is missing).

begin;

drop view if exists gitscout.repo_health;
drop view if exists gitscout.run_history;
drop table if exists gitscout.workers;
drop table if exists gitscout.alerts;
delete from gitscout.app_settings
 where key in ('alert_cooldown_minutes', 'overdue_hours', 'rate_alert_floor', 'worker_silent_minutes');

commit;
