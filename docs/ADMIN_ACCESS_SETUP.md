# Administrator Access Setup

How the maintainer (you) keeps **independently authenticated** access after handover,
using your own identity on each platform. Everything here is disclosed to the project
owner — collaborator lists, project members, and database roles all appear in their
dashboards. There are no hidden accounts or secret mechanisms, by design: disclosed
access is also the kind that survives the recipient changing their password.

> **Who runs these steps.** Inviting accounts, transferring repos, and assigning roles
> are owner actions performed in the GitHub/Supabase UI. The person with owner access
> must run them; you cannot grant yourself access, and neither can an automated agent.

---

## 1. GitHub

Two paths — pick one with the recipient (see `HANDOVER_GUIDE.md`). Verify actual
permissions in the UI **after** the action; don't assume a transfer preserved anything.

### Path A — repository transferred to the recipient

1. Recipient (new owner) goes to **Settings → General → Danger Zone → Transfer**, or you
   transfer it to them if you hold ownership at that moment.
2. New owner: **Settings → Collaborators and teams → Add people** → add your GitHub
   account with the **Admin** role (or **Maintain** if admin isn't required).
3. You accept the invite (email / GitHub notifications).

### Path B — recipient added to your repository

1. You keep ownership; **Settings → Collaborators → Add people** → add the recipient
   with **Admin**.
2. No change to your own access.

### Your authentication (both paths): SSH with your own key

```bash
# On your machine, once:
ssh-keygen -t ed25519 -C "gitscout-maintainer"      # private key stays local
cat ~/.ssh/id_ed25519.pub                            # add this at github.com → Settings → SSH keys
ssh -T git@github.com                                # expect "Hi <you>!"
git remote set-url origin git@github.com:<owner>/gitscout.git
git clone git@github.com:<owner>/gitscout.git        # verify clone works
```

- The **private** key never leaves your machine and is never shared with the recipient.
- Do not rely on the recipient's password or a personal access token belonging to them.
- Recovery if you lose the device/key: see `BACKUP_AND_RECOVERY.md`.

---

## 2. Supabase (recipient owns a new project)

The recipient creates and owns the production Supabase project. Your access is a
**membership tied to your own account**, independent of their password.

1. Recipient creates the project in **their** Supabase organization.
2. Recipient: **Organization → Team / Members → Invite** → your account email, with the
   least role that covers maintenance (start with **Developer**; use **Owner/Admin** only
   if project-config changes are part of the agreed duties).
3. You accept the invite and sign in to the Supabase Dashboard with **your own** account.
4. Confirm in the dashboard what your role actually allows (project settings, SQL editor,
   table/record management, migrations). Record the result in
   `ACCESS_VERIFICATION_CHECKLIST.md` — do not assume a role grants something you haven't
   verified.

### Database (CLI / psql) access — your own named role, not the shared DSN

The shared `SUPABASE_DB_URL` authenticates as `postgres` with the project DB password; a
password reset breaks it. For durable CLI/db access, the owner creates a **named role for
you** with its own password that you control and rotate:

```sql
-- Run by the project owner in the Supabase SQL editor.
-- Least-privilege maintenance role scoped to the gitscout mirror schema.
create role gitscout_maint login password '<set-a-strong-password-and-share-with-maintainer-only>';

grant usage on schema gitscout to gitscout_maint;
grant select, insert, update, delete
  on all tables in schema gitscout to gitscout_maint;
alter default privileges in schema gitscout
  grant select, insert, update, delete on tables to gitscout_maint;

-- Read-only on the host public schema (adjust to the agreed scope):
grant usage on schema public to gitscout_maint;
grant select on all tables in schema public to gitscout_maint;
```

- This role does **not** bypass RLS (only `service_role`/superuser does), so application
  security stays intact.
- You connect with a DSN built from **this role's** credentials, not the project
  `postgres` password — so a recipient password reset does not affect you.
- Rotate this role's password on your own schedule (`alter role gitscout_maint password
  '<new>'`); see `SECURITY_AND_SECRETS.md`.

### Updating the repo for the new project

- Change the project ref in `set-supabase-dsn.py` (`PROJECT_REF`) to the recipient's new
  ref, or override it via env. Regenerate `.env` with `python set-supabase-dsn.py`.
- Apply migrations to the new project in order: `migrations/0001 → 0004`.

---

## What breaks your access (stated honestly)

Your access remains subject to the owner's real permissions. If the owner **removes your
GitHub collaborator role or Supabase membership**, transfers the org, or deletes the
project, your access ends — there is no mechanism (and there should be none) that
survives that. This is the correct behavior and the reason a written maintenance
agreement matters (see `HANDOVER_GUIDE.md` and Phase 6 in the project brief).
