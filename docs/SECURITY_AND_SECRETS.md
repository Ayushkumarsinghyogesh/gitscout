# Security & Secrets

Rules for handling credentials in GitScout. The short version: secrets live in
environment variables or a secrets manager, never in the repository, never in client
code, and never pasted into source or ordinary docs.

## What the secrets are

| Secret | Purpose | Lives in | Scope |
|---|---|---|---|
| `GITSCOUT_GITHUB_TOKENS` | GitHub API access for scraping | GitHub Actions secret; local `.env` | PAT, `read:user` scope only |
| `SUPABASE_DB_URL` | Direct Postgres write (preferred) | local `.env` only | Server-side |
| `SUPABASE_KEY` (service_role) | REST write via PostgREST | local `.env` only | Server-side — **never** client |
| `SUPABASE_DB_PASSWORD` | Alt. to DSN; DSN assembled from it | local `.env` only | Server-side |
| `HUNTER_API_KEY` | Optional email enrichment | GitHub Actions secret; local `.env` | Server-side |

## Hard rules

1. **Never commit secrets.** `.env` is gitignored. Confirm with `git check-ignore .env`
   before any commit. A full-history scan at audit time found none committed — keep it
   that way.
2. **The `service_role` key is server-side only.** It bypasses Row Level Security, so it
   must never appear in a browser bundle, frontend env var, public repo, or client-side
   config. It travels only in an `Authorization` header from the GitScout process to the
   configured Supabase host, and is redacted from error output (`supabase.py`).
3. **Use the right key for the job.** `service_role` for the server-side sync (RLS is on
   precisely so the `anon` key cannot read other people's email addresses). Never ship
   `service_role` to anything a user's browser runs.
4. **Individual credentials, not a shared login.** The maintainer uses their own named
   database role (`gitscout_maint`) and their own GitHub/Supabase accounts — not the
   shared `postgres` superuser or a single borrowed login. This keeps actions
   attributable and lets one person's access be revoked without disturbing others.
5. **No secrets in docs.** This file names secrets; it never contains their values.

## Where secrets go, by environment

- **Local development / maintenance:** `.env` in the project root (gitignored). Build the
  Supabase DSN with `python set-supabase-dsn.py` so the password is correctly
  percent-encoded.
- **CI (GitHub Actions):** repository **Settings → Secrets and variables → Actions**.
  Already wired for `GITSCOUT_GITHUB_TOKENS` and `HUNTER_API_KEY`.
- **Production server/container (if added):** the platform's secrets manager or injected
  environment variables — not a file baked into an image.

## Rotation

Rotate on a schedule and immediately after any suspected exposure or at handover.

- **GitHub PAT:** regenerate at github.com → **Settings → Developer settings → Tokens**,
  update the Actions secret and local `.env`.
- **Supabase service_role / anon keys:** **Project Settings → API → Reset**. Update
  `.env` / server secrets afterward.
- **Supabase DB password (`postgres`):** **Project Settings → Database → Reset
  password**, then regenerate the DSN. Note this breaks any direct-DSN user — which is
  exactly why maintainer access uses a named role instead.
- **Maintainer DB role password:** `alter role gitscout_maint password '<new>';` — you
  control this independently.

## Issuing & revoking (summary)

- **Issue:** create the named role / invite the account with least privilege; hand the
  credential over a secure channel (password manager share, not email/chat).
- **Rotate:** reset the specific credential; update only the places that use it.
- **Revoke:** remove the GitHub collaborator, remove the Supabase member, and
  `drop role gitscout_maint;` (reassign/own objects first if it owns any).

## At handover — rotation checklist

- [ ] Rotate the Supabase DB password (invalidates the old `.env` DSN on the maintainer's
      old machine).
- [ ] Rotate `service_role` / `anon` keys if they were ever shared.
- [ ] Re-issue GitHub tokens under the new owner's account for CI.
- [ ] Confirm the old maintainer `.env` is deleted or now inert.
- [ ] Re-create the maintainer's named role on the new project with a fresh password.
