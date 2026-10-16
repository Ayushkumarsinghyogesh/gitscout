# Outreach data model

What the outreach team reads, where it comes from, and what each number means.

## Where to look

| Object | What it is |
|---|---|
| `"Defendermate2"."Defendermate-Github1-Trivy-Final"` | **Start here.** One row per person active in `aquasecurity/trivy`. |
| `"Defendermate2".final_outreach_users_trivy` | The same rows under the original name; both names work. |
| `"Defendermate2".final_outreach_users` | The same, for every registered repository. One row per (repository, person). |
| `gitscout.issue_detail` / `discussion_detail` / `pr_detail` | Full threads: complete description, every comment, each commenter's email. |
| `gitscout.people`, `person_emails`, `repositories` | Shared mirror tables: a person active in two repositories is one row. |
| `"Defendermate-Github1-Trivy".threads`, `comments`, `interactions` | Trivy's own rows. Every repository gets a schema named `"Defendermate-Github1-<Name>"`. |
| `gitscout.threads_all`, `comments_all`, `interactions_all` | Every repository's rows in one view, rebuilt when a repository is added. |

`Defendermate2` is quoted on purpose: it matches the existing `"Defendermate"` schema's
casing. Unquoted, Postgres folds it to `defendermate2`, which is a different schema.

```sql
select username, full_name, email, linkedin_url, company_name,
       issues_count, discussions_answered, pull_requests_count, last_seen_at
from "Defendermate2"."Defendermate-Github1-Trivy-Final"
where email is not null
order by discussions_answered desc, issues_count desc
limit 50;
```

The dataset has 42 columns. It is the facts GitScout can stand behind; it carries no
score, no verdict and no workflow state -- see *Columns that were removed* below.

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
object per thread, newest first. Since 2026-10-14 (migration 0008e) each object is the
thread plus **this person's own words in it** (migration 0009 added `author` to each of them), keys in this order:

| Key | What |
|---|---|
| `number`, `url`, `title` | The thread |
| `description` | The thread's **full** original post, Markdown intact. NULL means GitHub has none |
| `state` / `labels` (issues), `category` / `closed` / `is_answered` / `upvotes` (discussions), `state` / `merged` / `additions` / `deletions` (PRs) | Section-specific facts |
| `created_at`, `updated_at`, `author` | When, and who opened it |
| `role` | What this person did there: `author`, `commenter` or `reviewer` |
| `wrote_answer` (discussions) | Their comment was accepted as the answer |
| `own_comments` | How many entries `comments` holds |
| `comments_count` | GitHub's own total for the whole thread, everyone included |
| `comments` | **Only what this person wrote** in the thread, oldest first: their comments, discussion replies, reviews and inline code comments. Each: `type` (comment / reply / review / review_comment), `author` (always this person), `created_at`, `url`, `body` (full), `is_answer`, `review_state`, `file_path`, `reply_to` (the author a discussion reply answers), `parent_id`. `[]` for someone who only opened the thread |

The three columns are `json`, not `jsonb`, on purpose: `jsonb` stores keys shortest-first
(`url`, `role`, `state`, `title`, ...), `json` keeps the order above. Use `json_array_elements`
on them, or cast `::jsonb` for the `?` and `@>` operators.

Measured 2026-10-15: 50 MB of live data for 5,274 rows, the busiest maintainer's row 8 MB,
five rows over 1 MB. The file on disk can read up to twice that: a concurrent refresh
rewrites every changed row and leaves the old copies for the next refresh to reuse
(autovacuum marks them free; nothing needs doing). The Table Editor folds any cell over 10,240 characters (398 of the issue cells)
behind a "Load full value" button. That is a display cap, not truncation: SQL and the API
return the whole value. What the *other* people said in a thread is one query away:

| Want | Read |
|---|---|
| The full description of a thread | `gitscout.threads_all.body` (every line, Markdown intact; NULL means GitHub has no body, never that one was dropped) |
| Every comment, review, reply and inline code comment, with author, timestamps, URL | `gitscout.comments_all` (`comment_type` says which; `url` links to it on GitHub) |
| One person's threads with the whole conversation attached, each commenter's email alongside | `gitscout.issue_detail` / `discussion_detail` / `pr_detail` (`comments` is a JSON array of full objects) |
| The comments written by a thread's **opener** | `select * from gitscout.comments_all c join gitscout.threads_all t using (repo, kind, number) where c.author = t.author` |

**`own_comments` semantics, precisely:** inside a detail object it is the length of that
object's `comments`, which are *the row's person's* entries only -- not the thread
opener's, and not the repository owner's. On the mirror, `comment_count` on a thread is
GitHub's own total, `comments_stored` is how many are held, and `comments_truncated` is
true only while a gap remains (a run with `--fill` closes it).

```sql
-- one person's own messages in one issue, as rows
select d->>'number' as issue, c->>'type' as type, c->>'created_at' as at, c->>'body' as body
from "Defendermate2"."Defendermate-Github1-Trivy-Final" u,
     json_array_elements(u.issue_details) d,
     json_array_elements(d->'comments') c
where u.username = '06kellyjac' and d->>'number' = '2845'
order by c->>'created_at';
```

## Where `email` comes from, and why a LinkedIn link does not give one

There is no "LinkedIn email" anywhere in the pipeline, and there cannot be: LinkedIn
publishes no address through anything GitScout touches. `email` has exactly three
sources, all GitHub:

| `email_source` | What it is | Confidence |
|---|---|---|
| `gql_profile` | The address the person put on their own GitHub profile | 0.95 |
| `gql_contrib` | The author line of their commits in the target repository | 0.85 |
| `gql_commit` | The author line of commits in their own public repositories | 0.80 |

`linkedin_url` comes from the person's GitHub profile too (`socialAccounts`, or a
LinkedIn link typed into the website field). It is a second contact channel, not a
source of email. Measured on 2026-10-14 for trivy: 5,274 people, 3,433 with an email,
676 with a LinkedIn link, 117 with a LinkedIn link and no email. Every NULL email was
checked against the source tables: none is a lost value, all are addresses the person
never published. GitScout never guesses an address.

## Contact fields, and what they do not claim

| Column | Source | Caveat |
|---|---|---|
| `email` | Highest-confidence address found | Public profile, or the git author line of their own commits |
| `email_source` | `gql_profile`, `gql_contrib` or `gql_commit` | The confidence behind each source is in the table above; the number itself lives in `gitscout.people.email_confidence` |
| `email_verified` | -- | **Always NULL.** No deliverability check has been run. NULL means "not verified", never "valid" |
| `linkedin_url` | GitHub `socialAccounts`, or a LinkedIn URL typed into the profile's website field | Only what the person published. Canonicalised to `https://www.linkedin.com/in/<slug>` so it matches the CRM |
| `first_name` / `last_name` | Split from the display name | Only for plain multi-word names. Single words, commas or digits stay whole in `full_name` |
| `company_name` | Profile company field, `@` stripped | Free text, typed by the person |
| `company_domain` | The email's domain | **Only for work addresses.** Free-mail domains (gmail, outlook, qq, ...) give NULL |
| `company_website` | -- | Always NULL: nothing supports it without guessing |
| `estimated_utc_offset` | Posting-hour histogram | Measured on 173 people: median error 2 h, 71% within 3 h. A hint for a send window, not a fact |

## Columns that were removed (2026-10-14, migration 0008b)

Five columns of the first version are gone, on request: `relevance_score`,
`qualification_reason`, `email_confidence`, `outreach_status`, `data_quality_status`.
They were opinions layered on top of the facts; the facts stayed. Where each one went:

| Removed | Where the same answer now comes from |
|---|---|
| `relevance_score`, `qualification_reason` | Nowhere. Rank in the CRM or in the query (`order by discussions_answered desc, ...`); `gitscout.people.lead_score` still exists for the old crawler-side heuristic |
| `email_confidence` | `gitscout.people.email_confidence`, or the per-source table above |
| `outreach_status` | The CRM. Suppression is a join: `left join "Defendermate"."Do Not Contact List" d on lower(d.email) = lower(u.email) where d.email is null` (none of the listed addresses appears in the trivy data today) |
| `data_quality_status` | `where email is not null or linkedin_url is not null` and `where full_name is not null` say the same thing without a label |

Rollback (`migrations/0008_repo_schemas_and_outreach_v3.down.sql`) restores all five.

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

Add it to the profile and let the next scheduled run do the rest (see
`docs/ingestion-and-scheduling.md`), or by hand:

1. `gitscout repo-info owner/name` -- registers its stable id
2. Collect it: `gitscout scout owner/name --with-threads`
3. `gitscout supabase push` -- creates `"Defendermate-Github1-<Name>"` on first sight
   (`gitscout.ensure_repo_schema`), pushes, refreshes the dataset, and creates
   `"Defendermate2"."Defendermate-Github1-<Name>-Final"` (`gitscout.ensure_outreach_view`)

No new tables by hand, no SQL, no new ingestion code.
