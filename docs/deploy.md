# Deploying on Render

Everything runs in the Render project **defendermate-github-scraper**. The data stays in
Supabase; Render runs only code.

## Two layouts

| | Test (free) | Production (Starter) |
|---|---|---|
| Services | one web service, `gitscout-web`, with `GITSCOUT_EMBEDDED_WORKER=1` | `gitscout-web` and `gitscout-worker` (`render.yaml`) |
| Cost | $0 | $7 + $7 a month, plus $0.50 for the worker's 2 GB disk |
| Always on | no: it sleeps after 15 min without traffic | yes |
| Local working copy | `/tmp`, lost on restart; crawl cursors come back from Postgres | persistent disk |

**Free layout, how the nightly run still happens.** A sleeping service cannot fire its
own schedule, so Supabase wakes it. A `pg_cron` job (`gitscout-wake`) requests
`/healthz` at 23:58, 00:58 and 01:58 IST. That covers the 00:00 run, the Sunday 01:00
sweep and the 02:00 archive. While a job runs, the worker requests its own `/healthz`
every four minutes, so the service stays up until the work is done and then sleeps
again. It is not kept awake around the clock: the workspace's 750 free hours a month
are shared with the other free services in it.

**Moving to Starter.** Change both instance types to Starter in Render. Create the
worker from `render.yaml`, or as a Background Worker with `gitscout worker run`, a disk
at `/var/data` and `GITSCOUT_DB=/var/data/gitscout.db`. Then remove
`GITSCOUT_EMBEDDED_WORKER` from the web service, and unschedule the wake job:
`select cron.unschedule('gitscout-wake');`

## Environment

Secrets are set on the service, never in Git:

| Variable | web | worker |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Auth, Storage links | Storage uploads |
| `GITSCOUT_APP_DB_URL` | role `gitscout_app` | |
| `SUPABASE_DB_URL` | (embedded worker) | role `gitscout_writer` |
| `GITSCOUT_SESSION_SECRET` | generated | |
| `GITSCOUT_PUBLIC_URL` | the service URL | |
| `GITHUB_TOKENS` or `GITHUB_APP_*` | repository checks | collection |
| `RESEND_API_KEY` | invitations | alerts |

## Supabase Auth settings for the dashboard

Public signup is off, the site URL is the dashboard's, and passwords need 12 or more
characters. Accounts come only from invitations (see `docs/dashboard.md`).

## Retire the old schedulers

Once the Render worker has completed a night, disable the Windows scheduled task. The
GitHub Actions `scout.yml` must not push to the same project on the same night, or the
two runs spend GitHub points twice.
