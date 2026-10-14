# Outreach data model

What the outreach team reads, where it comes from, and what each number means.

## Where to look

| Object | What it is |
|---|---|
| `"Defendermate2".final_outreach_users_trivy` | **Start here.** One row per person active in `aquasecurity/trivy`. |
| `"Defendermate2".final_outreach_users` | The same, for every registered repository. One row per (repository, person). |
| `gitscout.issue_detail` / `discussion_detail` / `pr_detail` | Full threads: complete description, every comment, each commenter's email. |
| `gitscout.people`, `threads`, `comments`, `interactions`, `repositories` | The mirror the views are built from. |

`Defendermate2` is quoted on purpose: it matches the existing `"Defendermate"` schema's
casing. Unquoted, Postgres folds it to `defendermate2`, which is a different schema.

```sql
select username, full_name, email, linkedin_url, company_name, relevance_score,
       issues_count, discussions_answered, qualification_reason
from "Defendermate2".final_outreach_users_trivy
where data_quality_status = 'contactable'
order by relevance_score desc
limit 50;
```

## Why a materialized view

A plain view was tried first and measured: **69 s** to count 4,231 rows, because each
query rebuilt every person's JSON detail. Stored as a materialized view the same
count takes **under 2 s**.

It is refreshed at the end of every sync with `REFRESH MATERIALIZED VIEW CONCURRENTLY`.
The previous result stays readable for the whole refresh, so a slow or failed sync
never leaves an empty or half-built dataset. If a refresh fails the sync still
succeeds and the last good dataset keeps being served.

Materialized views cannot carry RLS. Access is by grants alone: nothing is granted to
`anon` or `authenticated`, the schema is revoked from both, and the project's Data API
is not exposed.

## What the counts mean

**Every count is this person's involvement in this repository** -- never a repository
total attributed to an individual.

| Column | Meaning |
|---|---|
| `issues_count` | Distinct issues in this repo the person **opened, commented on, or reviewed** |
| `issues_opened` | Of those, how many they opened |
| `discussions_count` | Distinct discussions they opened or commented on |
| `discussions_answered` | Discussions where **their comment was accepted as the answer** -- the strongest signal in the dataset |
| `pull_requests_count` | Distinct PRs they opened, commented on, or reviewed |
| `pull_requests_opened` / `pull_requests_reviewed` | Opened by them / reviewed by them |
| `forks_count` | Forks **they** own of this repository (0 or 1) |
| `repository_stars_count` / `repository_forks_count` | **Repository-wide** totals, the same for every row of a repo. Popularity context only |

The detail arrays (`issue_details`, `discussion_details`, `pull_request_details`) hold one
object per thread, newest first, with a 500-character `excerpt` of the description. The
full text and every comment are in `gitscout.issue_detail` and friends -- kept out of
here so one prolific maintainer cannot produce a multi-megabyte row.

## Contact fields, and what they do not claim

| Column | Source | Caveat |
|---|---|---|
| `email` | Highest-confidence address found | Public profile, or the git author line of their own commits |
| `email_source` / `email_confidence` | `gql_profile` 0.95, `gql_contrib` 0.85, `gql_commit` 0.80 | Below 0.80 is flagged `email_low_confidence` |
| `email_verified` | -- | **Always NULL.** No deliverability check has been run. NULL means "not verified", never "valid" |
| `linkedin_url` | GitHub `socialAccounts`, or a LinkedIn URL typed into the profile's website field | Only what the person published. Canonicalised to `https://www.linkedin.com/in/<slug>` so it matches the CRM |
| `first_name` / `last_name` | Split from the display name | Only for plain multi-word names. Single words, commas or digits stay whole in `full_name` |
| `company_name` | Profile company field, `@` stripped | Free text, typed by the person |
| `company_domain` | The email's domain | **Only for work addresses.** Free-mail domains (gmail, outlook, qq, ...) give NULL |
| `company_website` | -- | Always NULL: nothing supports it without guessing |
| `estimated_utc_offset` | Posting-hour histogram | Measured on 173 people: median error 2 h, 71% within 3 h. A hint for a send window, not a fact |

## Status columns

`outreach_status` is **derived, not stored**:

- `do_not_contact` -- the email or LinkedIn URL is on `"Defendermate"."Do Not Contact List"`
- `not_contacted` -- everyone else

The real workflow status belongs in the CRM. What this column guarantees is that nobody
on the do-not-contact list ever reads as contactable. (At the time of writing none of the
61 listed emails appears anywhere in the gitscout data.)

`data_quality_status`, first match wins:

| Value | Meaning |
|---|---|
| `suppressed` | On the do-not-contact list |
| `no_contact_channel` | Neither an email nor a LinkedIn URL |
| `email_low_confidence` | Has an email, confidence below 0.80 |
| `name_missing` | Contactable but no display name |
| `contactable` | Ready |

## Who is excluded

Bots and organisations are signals about a repository, never prospects: rows require
`account_type = 'User'` and a login not ending in `[bot]`, `-bot` or `_bot`.

## Identity and deduplication

- A person is their GitHub `login`. Two people with the same display name are two rows.
- One row per (repository, person), enforced by a unique index on
  `(repository_id, username)` -- the refresh fails rather than produce a duplicate.
- Repositories are identified by GitHub's `databaseId` (`repository_id`), which survives
  renames and transfers. Every mirror row joins the registry by `owner/name`.

## Adding a repository

1. `gitscout repo-info owner/name` -- registers its stable id
2. Collect it: `gitscout scout owner/name --with-threads`
3. `python supabase-load.py` -- pushes and refreshes the dataset
4. Optional per-repo view, one line:
   ```sql
   create view "Defendermate2".final_outreach_users_<name> with (security_invoker = true) as
   select * from "Defendermate2".final_outreach_users
   where repository_owner = '<owner>' and repository_name = '<name>';
   ```

No new tables and no new ingestion code.
