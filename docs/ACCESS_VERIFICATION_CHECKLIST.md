# Access Verification Checklist

Testable acceptance criteria for the handover. Fill in the result/date/owner as each is
verified. Do not mark handover complete until the required items pass. Run destructive
tests (password changes, deletes) only against a safe target and with permission.

## Acceptance criteria

| # | Criterion | How to verify | Result | Date / By |
|---|---|---|---|---|
| 1 | Recipient can access and administer the delivered GitHub repo | Recipient signs in, opens repo Settings, sees Admin controls | ☐ | |
| 2 | Maintainer's GitHub account has the agreed permissions | Repo → Settings → Collaborators shows maintainer with Admin/Maintain | ☐ | |
| 3 | Maintainer can clone & maintain without the recipient's password | `git clone git@github.com:<owner>/gitscout.git` over maintainer SSH key; create a test branch | ☐ | |
| 4 | Maintainer's Supabase account is authorized for the agreed tasks | Maintainer signs in to the recipient's project; confirms SQL editor / table access per role | ☐ | |
| 5 | Recipient password change does NOT break maintainer auth | In a safe test: recipient resets their Supabase password → maintainer's own-account login and named DB role still work | ☐ | |
| 6 | Maintainer DB permissions are sufficient but not broader than needed | Maintainer can select/insert/update/delete in `gitscout` schema; cannot act as superuser / bypass RLS | ☐ | |
| 7 | RLS and application security intact | `anon` key cannot read `person_emails`; `security_invoker` views honor caller privileges | ☐ | |
| 8 | No hidden accounts, backdoors, undocumented access, or committed secrets | `git grep`/history scan clean; every access path listed in `ADMIN_ACCESS_SETUP.md` is visible in the owner's dashboards | ☐ | |
| 9 | Backup & restore procedure documented and tested safely | `pg_dump` taken and restored to a scratch target per `BACKUP_AND_RECOVERY.md` | ☐ | |
| 10 | Recipient has a simple non-programmer handover guide | `HANDOVER_GUIDE.md` reviewed with recipient | ☐ | |
| 11 | Technical admin guide covers setup, permissions, troubleshooting, revocation, recovery | `ADMIN_ACCESS_SETUP.md` + `SECURITY_AND_SECRETS.md` reviewed | ☐ | |
| 12 | All changes documented, reviewable, reversible where feasible | Docs committed; access changes logged below | ☐ | |

## Verification commands (copy/paste)

```bash
# 2/3 — GitHub access as the maintainer
ssh -T git@github.com                      # expect: Hi <maintainer>!
git clone git@github.com:<owner>/gitscout.git && cd gitscout && git checkout -b verify-access

# 7 — RLS: anon key must NOT read protected rows (expect empty / error, not data)
curl -s "$SUPABASE_URL/rest/v1/person_emails?select=email&limit=1" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Authorization: Bearer $SUPABASE_ANON_KEY"

# 6 — maintainer role scope (run as gitscout_maint): write should work, superuser should not
psql "$MAINT_DSN" -c "insert into gitscout.sync_runs default values; delete from gitscout.sync_runs where false;"
psql "$MAINT_DSN" -c "show is_superuser;"   # expect: off
```

## No-backdoor attestation

At handover the maintainer attests, and the recipient can independently confirm, that:

- Every maintainer access path is a **named, visible** entry in the owner's GitHub
  collaborator list and Supabase member/role lists.
- No additional database users, API endpoints, service-role embeds, scheduled jobs, or
  credentials exist beyond those documented here.
- The recipient can remove all maintainer access themselves (criteria 8) and doing so
  fully ends that access.

## Access change log

| Date | Change (grant / revoke / rotate) | Platform | By | Notes |
|---|---|---|---|---|
| | | | | |
