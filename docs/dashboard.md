# Dashboard

`gitscout web` serves the dashboard. Pages are rendered on the server and contain no
JavaScript. Supabase Auth handles sign-in; the role in `gitscout.app_users` decides
what each account can do.

## Roles

| Role (in the database) | Shown as | Can |
|---|---|---|
| `platform_admin` | Super admin | everything, on every repository; invite anyone; disable accounts |
| `client_admin` | Admin client | on their repositories: everything a client can, plus pause or resume nightly runs, cancel runs, invite clients, remove access |
| `client_viewer` | Client | see their repositories' records and runs, add repositories, press Run now, download CSV |

A client or admin client sees only the repositories in their memberships. Every query
is scoped in `dashboard/data.py`, so the scope is applied in one place. A repository
someone adds is visible to them right away. Adding one that GitScout already tracks
gives them access to it; it is not collected twice. Non-super-admins can add up to
`max_repos_per_user` (10) repositories.

## Pages

- **Overview**: people, emails, LinkedIn, repositories and failing runs; each
  repository with its status and next run; recent runs. Super admins also see workers
  and alerts.
- **Repositories**: add repositories by pasting `owner/name` or links, or by uploading
  a .txt or .csv list. Each new one is checked on GitHub (private repositories are
  refused), registered, and queued, so collection starts within a minute. After that
  the nightly 00:00 IST run picks up only what is new. Each repository has Run now,
  Pause, its run history and an export form.
- **Records**: search by name, username, email or company; filter by repository or
  "with email"; newest collected first. Each person's page shows contact details,
  involvement counts and their own comments in each thread.
- **Export CSV**: download now, either every record to date or a From/To range,
  filtered by when GitScout collected the person or when they were active on GitHub.
  The CSV streams to the browser and is saved on the viewer's machine. The page also
  lists the 02:00 IST nightly archives; each link is a signed URL valid for 5 minutes.
- **Runs**: every job with status, trigger, duration and what it collected; cancel.
- **Users**: invite by email with a role and repositories; disable; remove access.

## Sign-in and invitations

Nobody can sign up. The only ways in are `gitscout admin invite <email> --role
platform_admin` (bootstrapping the first super admin) and the Users page. Both create
the Auth user through `generate_link`, write the `app_users` row on the server, and
email a link to `/auth/confirm` on the dashboard. That page needs one click before the
token is spent, so a mail scanner opening the link does not use it up. The person
then chooses a password of 12 or more characters. Without `RESEND_API_KEY`, the inviter
is shown the one-time link to pass on, and the CLI writes it to a mode-600 file instead.

The session is a signed, HTTP-only, `Secure`, `SameSite=Lax` cookie holding the Auth
tokens. They are validated with Auth on every request (cached for 60 s) and refreshed
before they expire. Every form carries a CSRF token. Responses send a CSP with
`script-src 'none'`, `X-Frame-Options: DENY` and HSTS. A disabled account, or one
without an `app_users` row, is signed out on its next request.

## Configuration

| Variable | |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Auth and Storage, server side only |
| `GITSCOUT_APP_DB_URL` | Postgres as `gitscout_app` (falls back to `SUPABASE_DB_URL`) |
| `GITSCOUT_SESSION_SECRET` | 32+ random characters (Render generates it) |
| `GITSCOUT_PUBLIC_URL` | the dashboard's address, used in emailed links |
| `RESEND_API_KEY`, `GITSCOUT_ALERT_FROM` | invitation and reset emails |
| `GITHUB_TOKENS` | optional: checks repositories with a token instead of anonymously (60/h) |

Migration 0015 adds `memberships.revoked_at`, the `max_repos_per_user` setting and the
`gitscout.repo_stats` view. `render.yaml` deploys the dashboard and the worker together.
