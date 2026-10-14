# Backup & Recovery

Procedures for backing up the project and recovering from lost access, lost keys, or
accidental data loss. Test these **before** handover is considered complete, using a
safe (non-production or confirmed) target.

## What needs backing up

| Asset | Where it is | Backup method |
|---|---|---|
| Code | GitHub repo | Git itself (every clone is a full copy); optional mirror |
| Crawl state | `state/gitscout.db` (SQLite) | GitHub Actions cache per run; copy the file |
| Mirrored results | Supabase `gitscout` schema | `pg_dump` / Supabase backups |
| Schema & migrations | `migrations/*.sql` in the repo | Git |
| Secrets | `.env`, Actions secrets | Password manager (never in git) |

## Code backup

A Git repository is already distributed — each clone is a complete backup. For an extra
off-platform copy:

```bash
git clone --mirror git@github.com:<owner>/gitscout.git gitscout-mirror.git
# store gitscout-mirror.git somewhere safe; refresh with: git -C gitscout-mirror.git remote update
```

## Database backup (Supabase)

**Managed (recommended):** Supabase **Project → Database → Backups**. Paid tiers include
daily automated backups and point-in-time recovery; the free tier does not, so take
manual dumps.

**Manual dump** (uses your own named role / authorized DSN — never commit the output):

```bash
# Whole gitscout mirror schema:
pg_dump "$SUPABASE_DB_URL" --schema=gitscout --no-owner --no-privileges -f gitscout-backup.sql
# Restore into a target project:
psql "$TARGET_DSN" -f gitscout-backup.sql
```

Store dumps encrypted and outside the repo. They contain personal data — treat them like
the database itself.

## Rebuilding the mirror from scratch

If the Supabase data is lost but SQLite (`gitscout.db`) survives, the mirror can be
rebuilt — SQLite is the source of truth:

1. Create/select the Supabase project.
2. Apply migrations in order: `0001 → 0002 → 0003 → 0004`.
3. Re-run the sync: `gitscout` push to Supabase (see the CLI / `supabase.py`). The sync
   is idempotent (upsert on conflict), so re-running is safe.

## Account & key recovery

### Lost SSH key / device (GitHub)
1. From any machine where you're still signed in to GitHub, remove the lost key at
   **Settings → SSH keys**.
2. Generate a new key (`ssh-keygen -t ed25519`), add the public half, re-test
   `ssh -T git@github.com`.
3. If you've lost access entirely, the repo owner re-invites your account as a
   collaborator — your access is not tied to any single device.

### Lost Supabase access
- Dashboard: use your account's normal password reset. Your **membership** is unaffected
  by the owner's password.
- Database role: the owner resets it — `alter role gitscout_maint password '<new>';`.
- If your membership was removed, only the owner can re-invite you (by design).

### Compromised credential
Follow `SECURITY_AND_SECRETS.md` → Rotation immediately for the specific credential, then
review Supabase logs / GitHub audit log (where the plan supports it) for unexpected use.

## Accidental data deletion

- **Before any bulk change or delete:** take a fresh `pg_dump` (above) and confirm it
  restores to a scratch target. Require explicit confirmation for bulk operations.
- **After an accidental delete:** restore the affected tables from the most recent dump,
  or re-run the idempotent sync from SQLite if the loss is in the mirror only.
- Prefer soft deletes / `where` filters over unbounded `delete`; never run `delete`
  without a `where` clause on these tables.

## Test log

Record each test (date, who, target, result) in `ACCESS_VERIFICATION_CHECKLIST.md` so the
procedures are demonstrably working, not just written down.
