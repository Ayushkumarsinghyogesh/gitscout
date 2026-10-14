# Handover Guide (for the new owner)

Welcome — this guide is written for a non-programmer. It explains, in plain language,
what you are receiving, what you need to do, and who can access your data. You can hand
the technical steps to anyone you trust; this guide tells you what should happen and
why.

## What GitScout is

It's a tool that gathers public information about GitHub projects and the people active
in them, and organizes it into a contact list for outreach. It stores the results in a
database (Supabase). It does not post anything or email anyone by itself.

## What you're receiving

1. **The code** — a GitHub repository.
2. **The database** — a Supabase project that holds the organized results.
3. **These documents** — setup, security, backup, and a checklist.

## What you need to set up (one time)

1. **A GitHub account** (free). You'll either receive the repository into your account or
   be added to it. A technical helper can do the actual clicks; the steps are in
   `ADMIN_ACCESS_SETUP.md`.
2. **A Supabase account and project** (free tier is fine to start). You create the
   project and own it. The database structure is applied from the files in the
   `migrations/` folder.
3. **Two secret values**, kept private (never emailed in plain text, never put in the
   code): a GitHub access token and the Supabase keys. `SECURITY_AND_SECRETS.md`
   explains where they go.

## About continued maintainer access — please read

The person handing this over (the maintainer) will keep **limited, named access** so they
can fix problems, run updates, and help recover data. This is **not hidden**: you will be
able to see them listed as a collaborator on your GitHub repository and as a member of
your Supabase project, and you can remove them at any time.

- Their access uses **their own login**, separate from your password. Changing your
  password does not lock them out — and that's intended, so maintenance keeps working.
- You stay the **owner**. If you remove their collaborator role or project membership,
  their access ends. Nothing survives that removal.
- We recommend a short **written maintenance agreement** (see below) so everyone knows
  what access exists, what it's for, and how to end it.

If you ever want the maintainer's access removed, you do it yourself:
- GitHub: **Settings → Collaborators →** remove them.
- Supabase: **Organization → Members →** remove them, and drop their database role.

## Day-to-day: who to call

- The tool runs on a schedule by itself (GitHub Actions). You don't need to start it.
- If something looks wrong, the maintainer can inspect it with their own access.
- To stop all collection: disable the "Scout" workflow in the GitHub repository's
  **Actions** tab.

## Your data responsibilities (brief)

The database contains contact information about real people. Handle it in line with the
laws that apply to you (for example, honoring opt-out/deletion requests). The maintainer
can help you correct or delete records — see `BACKUP_AND_RECOVERY.md`.

## Suggested maintenance agreement (one page, plain terms)

Put in writing, signed by both sides:

- **Who owns what:** you own the GitHub repo, the Supabase project, the billing, and the
  data.
- **What access the maintainer keeps:** named GitHub collaborator (Admin/Maintain) and a
  named Supabase member + database role, for maintenance only.
- **What they'll do:** fixes, updates, migrations, backups, recovery help.
- **Data handling:** they won't export or use the data outside the agreed maintenance.
- **How access ends:** either side can end it; you remove their roles and they confirm.
- **Review date:** revisit, say, every 6 or 12 months.
