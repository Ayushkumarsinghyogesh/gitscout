# Ingestion and scheduling

## One command is the whole pipeline

```
gitscout scout -t cloud-security --incremental --with-threads --with-social --push
```

What one run does, in order:

| Step | What | Cost (trivy, steady state) |
|---|---|---|
| 1 | Read the profile: every uncommented `[[target]]` in `src/gitscout/profiles/cloud-security.toml` | -- |
| 2 | Interactions, incremental: forks, issues, PRs, discussions, commits since the last run | ~5 points |
| 3 | Threads, incremental (`UPDATED_AT` order): new and *touched* issues/PRs/discussions with bodies, labels, comments, reviews, inline code comments, replies | ~30 points |
| 4 | Fill: any thread the page cap left incomplete is paged to the end | 0 unless a megathread grew |
| 5 | Participants: anyone seen only inside a thread gets a profile | ~1 point / 25 |
| 6 | Email discovery for new people (profile, then commit author lines) | ~1 point / 10 |
| 7 | Social: LinkedIn link + public repo count from their own profile | ~1 point / 25 |
| 8 | Registry: each repo's stable id refreshed | 1 point / repo |
| 9 | Push: one Postgres transaction as `gitscout_writer`; repo-scoped rows go to `"Defendermate-Github1-<Name>"`, shared rows to `gitscout` | ~3 s when nothing changed; ~4 min for a full re-push of 77,000 rows |
| 10 | Refresh `"Defendermate2".final_outreach_users` (concurrently: readers never see an empty table) | ~12 s |

A full re-crawl of trivy costs about 1,600 GraphQL points; the hourly budget is 5,000 per
token. If the budget runs out mid-run the client sleeps until it resets and continues.

## What makes it safe to run at any time

- **Incremental.** Cursors and high-water marks live in the local SQLite file
  (`crawl_state`); a run reads only what changed. The push has its own watermark
  (`supabase:sync`), which only advances after every table has committed.
- **Idempotent.** Every write is an upsert on a stable key (login; repo+kind+number;
  comment id). Re-running changes nothing that already landed.
- **Locked.** `--push` takes `gitscout.db.lock` next to the database. A second run exits
  with code 3 instead of racing. A lock whose process has died is taken over.
- **Atomic.** Data lands in one transaction or not at all; a failed refresh keeps the
  previous dataset serving and is retried next run (`supabase:refresh`).
- **Least privilege.** `gitscout_writer` can insert/update the mirror and execute two
  functions. It cannot delete, truncate, create tables, schedule jobs, or read any other
  schema -- each of these was tested and is refused.

## The job queue (migration 0011)

Runs are rows in `gitscout.jobs`. A row is created `queued` (by the schedule, by a
person, or as a retry), claimed by exactly one worker, and ends `succeeded`, `failed` or
`cancelled`. The row keeps the run's history: who asked, when it started and finished,
counters per collection, the checkpoint it reached, GitHub points used, the worker and
code version, and a sanitized error.

| Guarantee | How |
|---|---|
| Never two runs of one repository and kind | A partial unique index allows one queued-or-running row per (repo, kind); the second enqueue is refused, not raced |
| Two workers never take the same job | `claim` uses `FOR UPDATE SKIP LOCKED` |
| A dead worker does not hold a job forever | The worker heartbeats every 30 s; a running job silent for 10 min is failed as stalled and retried |
| Failures retry on their own | Up to 3 attempts, exponential backoff with jitter (30 s to 30 min); each retry is a new row pointing at the failed one |
| A lost worker disk does not mean a re-backfill | The worker copies its cursors into `repositories.checkpoint` after every run |

Repository configuration lives in `gitscout.repositories`: `enabled`, `collections`,
`schedule_cron` evaluated in `timezone` (default: every night at 00:00 Asia/Kolkata),
and per-repo `settings` such as `{"max": 2000, "limit": 2000}`. The profile TOML is
only a seed for registering a repository.

```bash
gitscout repos list                                   # schedules and last success
gitscout repos set owner/name --schedule "0 0 * * *" --tz Asia/Kolkata --max 2000
gitscout repos set owner/name --disable               # pause; data stays
gitscout jobs enqueue owner/name --kind full          # ask for a run now
gitscout jobs list --status failed                    # what went wrong, and why
gitscout jobs cancel 42
```

`gitscout_app` is the dashboard's database role: it reads everything, writes only the
application tables and the configuration columns, and can delete nothing. Like the
writer it is created without a password; the password is set once, out of band.

## The worker (production)

One process, `gitscout worker run`, is the whole production pipeline. Every minute it:

1. fails any running job whose heartbeat stopped (worker died) and queues its retry;
2. enqueues an `incremental` job for every enabled repository whose cron matches this
   minute, in that repository's time zone;
3. claims the next due job and runs it: crawl → enrich → social → push → dataset
   refresh, heart-beating every 30 s; then records counters, checkpoint, GitHub points
   and any error on the job row.

Nothing else needs to run, and nothing is held in memory: kill the process at any
moment and the next start carries on from the queue. Two workers may run side by side.

| Variable | What |
|---|---|
| `SUPABASE_DB_URL` | the `gitscout_writer` pooler DSN: queue and mirror |
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` | the GitHub App: installation tokens are minted and renewed by the worker, so nothing ever expires. Multi-line PEM may be pasted with `\n` |
| `GITHUB_TOKENS` | alternative to the App: static personal tokens |
| `GITSCOUT_DB` | the local crawl store on the worker's persistent disk (e.g. `/data/gitscout.db`) |
| `GITSCOUT_WORKER_POLL`, `GITSCOUT_WORKER_HEARTBEAT` | seconds; defaults 60 and 30 |
| `RESEND_API_KEY` | alert email (optional; without it alerts are recorded, not mailed) |
| `GITSCOUT_ALERT_FROM` | sender, default `GitScout <alerts@notify.defendermate.com>` |
| `GITSCOUT_ALERT_EMAILS` | fallback recipients when `app_settings.alert_emails` is empty |
| `GITSCOUT_LOG_FORMAT` | `json` for one JSON object per line with `job_id`, `repo`, `kind` |
| `GITSCOUT_VERSION` or `RENDER_GIT_COMMIT` | recorded on every job as `code_version` |

Run it by hand for one pass (`--once`): recover, schedule, run at most one job, exit.
To ask for a run outside the schedule: `gitscout jobs enqueue owner/name` (or the
dashboard); the worker picks it up within a minute. A manual `gitscout scout --push` on
the worker's machine is refused while a job holds the run lock, and vice versa.

If the worker's disk is lost, the first job restores each repository's cursors from
`repositories.checkpoint` and continues incrementally; only the local enrichment memory
is rebuilt over the following runs, bounded by `--limit`.

## Deleted threads and administrator exclusions (migration 0012)

An incremental crawl cannot see an absence: an issue that was deleted, converted or
transferred simply stops being listed. Once a week (Sunday 01:00 in the repository's
time zone, `settings.sweep_cron` to change it) the worker runs a `sweep` job: it lists
every thread number GitHub still has (one point per hundred, about 110 points for
trivy), compares with what is stored, and sets `threads.source_deleted_at` on the
difference. Nothing is deleted: the row, its comments and its history stay; the
outreach dataset leaves the thread out. A thread that is listed again is un-marked.
A listing that does not reach the last page marks nothing for that kind and the job
fails, so a network cut can never make everything after it look deleted.

Decisions a person makes live in `gitscout.exclusions`, not in the synced rows, so no
sync can undo them: kind `person` with the login, or kind `thread` with
`<section>/<number>`. The dataset filters on them at refresh; set `removed_at` to lift
one. The dashboard's delete and exclude actions write this table (Phase 8).

Comment-level deletions are not detected: a comment removed from a thread that keeps
existing stays stored until the thread is next re-fetched because it changed.

## Alerts and health (migration 0013)

Nobody watches the worker, so it reports on itself.

**Alerts.** After every job and every tick the worker checks these rules. Each alert
is written to `gitscout.alerts` and emailed through Resend. One key sends at most one
email per `alert_cooldown_minutes` (default 360).

| key | severity | when |
|---|---|---|
| `job_failed:<repo>:<kind>` | critical | a job failed with no attempts left |
| `auth` | critical | GitHub refused the credentials |
| `stalled:<repo>` | warning | a running job stopped heart-beating |
| `rate:<repo>` | warning | a run ended with fewer than `rate_alert_floor` (250) points left |
| `refresh:<repo>` | warning | data landed, dataset refresh failed |
| `overdue:<repo>` | warning | enabled, no success for `overdue_hours` (26) |
| `recovered:<repo>:<kind>` | info | the first success after a `job_failed` alert |

Recipients come from `app_settings.alert_emails`, a JSON list. Set them once:

```sql
update gitscout.app_settings set value = '["ayushkumarsinghyogesh@gmail.com"]' where key = 'alert_emails';
```

`gitscout alerts test` sends a test alert and says whether it went out.

**Liveness.** `gitscout.workers` holds one row per worker process. `seen_at` is refreshed
every tick and every heartbeat, and `stopped_at` is set on a clean shutdown.

**Views.** `gitscout.run_history` has one row per job, with duration, headline counts
and GitHub points. `gitscout.repo_health` has one row per repository: last success,
consecutive failures, last and active job, and an `overdue` flag.

**`gitscout health`** prints workers, repositories and recent alerts. It exits 1 when
a repository is overdue or no worker was seen within `worker_silent_minutes` (15), so
an external check can run it. **`gitscout jobs show <id>`** prints everything recorded
for one job.

A dead worker cannot email about its own death. The `overdue` alert comes from the
next worker that runs, and Render restarts a crashed process. A check that runs
independently of the worker comes with the dashboard (Phase 8).

## Exports (migration 0014)

**One file format.** Every export is a CSV of the outreach dataset: all its columns,
plus `first_collected_at`. It starts with a UTF-8 BOM so Excel shows accents and emoji.
JSON columns stay JSON text, and lists are joined with `;`. Text that starts with
`= + - @` gets a leading `'`, because text from GitHub could otherwise run as a
spreadsheet formula.

**Date range.** `--from` and `--to` are whole days, both included, in `--tz` (default
Asia/Kolkata). `--by` chooses which date is filtered:

- `collected` (default): when GitScout first put the person into the dataset
  (`gitscout.dataset_first_collected`). `refresh_outreach()` keeps this up to date.
- `activity`: the person's GitHub activity in the repository overlaps the range.

**Excel-safe.** Excel cannot hold more than 32,767 characters in one cell. About 320 trivy
people have a longer thread history, up to 15 MB. By default those cells are shortened
to 32,000 characters and end with a marker giving the full length. `--full` keeps every
character.

```bash
gitscout dataset export --repo aquasecurity/trivy                                  # everything to date
gitscout dataset export --repo aquasecurity/trivy --from 2026-10-01 --to 2026-10-15
gitscout dataset export --by activity --from 2025-01-01 --to 2025-12-31 --full -o 2025.csv
```

The dashboard's Export button (Phase 8) streams the same file to the browser, so it
downloads to the viewer's machine.

**Nightly archive.** An `export` job runs at 02:00 in each repository's time zone
(`settings.export_cron` overrides it). It writes the complete all-time CSV, gzipped,
to the private Storage bucket `gitscout-exports` at `<repo>/<yyyy>/<mm>/...csv.gz` and
records it in `gitscout.exports`. In the same job, files older than
`export_retention_days` (90) are deleted from Storage, and their rows are kept with
`deleted_at` set. This needs `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` on the worker.
`gitscout dataset archives` lists the archives, and `gitscout dataset fetch <id>`
downloads one.

## The schedule

Windows Task Scheduler, registered by `scripts/install-schedule.ps1`:

| Setting | Value | Why |
|---|---|---|
| Task | `gitscout-scheduled-sync` | |
| Runs | daily 09:07 and 21:07 local (`-EveryHours 24` for once a day) | off the hour, inside a working day so the PC is likely on |
| Action | `wscript.exe scripts\scheduled-sync-hidden.vbs` → `scheduled-sync.ps1` | no console window: the first trial run was killed when its window was closed |
| Missed run | starts when the PC is next on | runs are incremental, nothing is lost |
| Overlap | `IgnoreNew` + the run lock | never two runs on one database |
| Time limit / retries | 3 h / 2 × 15 min | a hung run is stopped; a network blip is ridden out |
| Logs | `logs\sync-<timestamp>.log`, newest 60 kept | `exit=3` in a log means another run held the lock |

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1            # register / update
Enable-ScheduledTask  -TaskName gitscout-scheduled-sync                           # it is disabled during the refactor
Start-ScheduledTask   -TaskName gitscout-scheduled-sync                           # run now
Get-ScheduledTaskInfo -TaskName gitscout-scheduled-sync | fl LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult` 0 = success, 3 = lock held (not a failure), `3221225786` (0xC000013A) =
the process was killed by a console close.

### The other scheduler

`.github/workflows/scout.yml` also runs twice a day (02:07 / 14:07 UTC) on GitHub
Actions. **It is a separate data island:** it crawls into its own Actions-cache SQLite
file and uploads CSV artifacts; it never pushes to Supabase (it has no database
credentials, and it should not -- the writer DSN would then live in GitHub). It is
harmless, free, and a useful smoke test that the crawl still works; it is not the
production pipeline. Disable it if the duplicate crawl is unwanted.

## Adding a repository

1. Uncomment or add a `[[target]]` in `src/gitscout/profiles/cloud-security.toml`.
2. Nothing else. The next run registers it (`gitscout repo-info`), creates
   `"Defendermate-Github1-<Name>"` with its three tables from the trivy template
   (`gitscout.ensure_repo_schema`), rebuilds the union views, crawls it from scratch,
   pushes, and creates `"Defendermate2"."Defendermate-Github1-<Name>-Final"`.

The first run for a large repository is a backfill and costs its full size in points;
use `--max N` on a manual run to bound it, then let the schedule continue incrementally.

## Credentials

All in `.env`, which is gitignored: `GITHUB_TOKENS` (PAT with `read:user`),
`SUPABASE_DB_URL` (the `gitscout_writer` pooler DSN). Nothing is written to logs: the
writer redacts the DSN from every error it raises.
