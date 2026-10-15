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
