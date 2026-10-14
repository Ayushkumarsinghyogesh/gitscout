# Access Control Audit

_Snapshot taken before handover. Read-only inspection; no settings were changed to produce it._

## Summary

GitScout collects GitHub repository and community data and mirrors results into a
Supabase (Postgres) project for outreach. Secret hygiene is good: no credentials are
committed and none appear in git history. The main handover risks are (1) a live `.env`
on the current maintainer's machine, (2) a hardcoded Supabase project ref in a helper
script, and (3) the fact that direct-DSN database access is **not** independent of the
Supabase database password and therefore cannot be the maintainer's durable access path.

## GitHub

- **Owner of record:** the `Ayushkumarsinghyogesh` personal GitHub account.
- **Remote:** `https://github.com/Ayushkumarsinghyogesh/gitscout.git`.
- **Automation:** `.github/workflows/scout.yml` (scheduled scrape, cron `7 2,14 * * *`)
  and `.github/workflows/ci.yml`. The scout workflow reads `GITSCOUT_GITHUB_TOKENS` and
  `HUNTER_API_KEY` from **GitHub Actions secrets** — not committed. Workflow permission
  is `contents: read`.
- **Branch protection / required reviews:** configured in the GitHub UI, not visible in
  the repo. Verify in **Settings → Branches** before handover.
- **Dependency on recipient's account/password:** none today (you own it). After
  handover this flips — see `ADMIN_ACCESS_SETUP.md`.

## Supabase

- **Project ref:** `nqnocdkwdnvczxuvhcvr` (hardcoded in `set-supabase-dsn.py`). A project
  ref is semi-public — it appears in every project URL — so this is not a secret, but it
  ties the repo to one specific project and must change when the recipient owns a new one.
- **Mirror schema:** `gitscout` (tables `people`, `person_emails`, `threads`,
  `comments`, `sync_runs`, `repositories`, `interactions`). The host project's `public`
  schema is left untouched by design.
- **Outreach views / materialization:** `migrations/0001`–`0004` build
  `final_outreach_users` (view, then materialized). Views are declared
  `security_invoker = true`, so they run with the **caller's** privileges, not the view
  owner's — users cannot escalate through them.
- **RLS:** enabled on every data table. Application/anon access is therefore denied by
  default; writes rely on the `service_role` key deliberately bypassing RLS.
- **Transports:** two writers (`config.py`):
  - `SUPABASE_DB_URL` — direct Postgres DSN (preferred; works with PostgREST disabled).
  - `SUPABASE_URL` + `SUPABASE_KEY` (service_role) — REST via PostgREST.

## Secrets & credentials

| Item | Where it lives | Committed? | Notes |
|---|---|---|---|
| GitHub tokens | GitHub Actions secret `GITSCOUT_GITHUB_TOKENS`; local `.env` | No | PAT with `read:user` scope |
| Supabase DSN / DB password | local `.env` (`SUPABASE_DB_URL`) | No | **Live password on disk — do not share; rotate at handover** |
| Supabase service_role key | local `.env` (`SUPABASE_KEY`) | No | Server-side only; redacted from errors |
| Hunter API key | GitHub Actions secret; local `.env` | No | Optional enrichment |

- `.gitignore` covers `.env`, `*.db`, `*.csv`, `*.jsonl`, `out/`, `state/`, plus
  `note.txt` / `personaldocs.txt`.
- **Full-history scan:** no connection strings with embedded passwords and no API-key
  literals found in any commit. The only `service_role` hits are code and documentation
  references, not secrets.

## Backup / restore / migration (current state)

- **SQLite** (`gitscout.db`) is the source of truth for crawl cursors/high-water marks.
  In CI it is cached per run (`actions/cache`) under `gitscout-db-<run_id>`.
- **Supabase** holds the mirrored results; migrations are forward/`.down.sql` pairs under
  `migrations/`.
- No automated off-box backup of the Supabase project is configured yet — see
  `BACKUP_AND_RECOVERY.md`.

## Risks & required actions at handover

1. **Live `.env` on the maintainer machine.** Must not be delivered to the recipient;
   rotate the Supabase DB password and any shared tokens after handover so the old copy
   is inert.
2. **Hardcoded project ref** in `set-supabase-dsn.py`. Update to the recipient's new
   project ref (or make it a prompt/env value) when they stand up their project.
3. **Direct-DSN access is password-coupled.** The DSN authenticates as the `postgres`
   user with the project DB password; a recipient password reset breaks it. Durable
   maintainer access must come from **project membership + a dedicated named DB role**,
   not the shared DSN. See `ADMIN_ACCESS_SETUP.md`.
4. **Branch protection / member lists** are UI-only state — verify directly after any
   transfer rather than assuming it carried over.
