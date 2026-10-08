# gitscout

Find the GitHub users who **star, fork, file issues, open pull requests, post in
discussions or land commits** on repos you care about, then discover their **public
emails**. Built on the official GitHub **GraphQL API**, designed to run on a **cron**.

```
scout  =  ingest (interaction + profile in one query)
       ->  enrich (commit emails, batched)
       ->  score  (0-100 lead score)
       ->  export (CSV + JSONL)
```

Targeted at open-source cloud security audiences out of the box: the shipped
`cloud-security` profile watches Prowler, Checkov, Trivy, Falco, OPA, Steampipe,
Gitleaks, trufflehog and ~15 more.

---

## Quick start

```bash
# 1. install (Python 3.11+)
python -m venv .venv && .venv\Scripts\activate       # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# 2. add a GitHub token -- this is REQUIRED, see below
cp .env.example .env            # then edit: GITHUB_TOKENS=ghp_...

# 3. prove it works against live GitHub
gitscout doctor

# 4. collect
gitscout scout --targets cloud-security --max 1000 -o out/leads.csv --only-with-email
```

> **A token is mandatory.** GitHub now returns `401 Unauthorized` for anonymous API
> requests — verified directly:
> ```
> $ curl -s -o /dev/null -w '%{http_code}' \
>     https://api.github.com/repos/prowler-cloud/prowler/stargazers
> 401
> ```
> Create one at <https://github.com/settings/tokens> and **tick `read:user`**.
> One token = 5,000 GraphQL points/hour.
>
> `read:user` matters: GraphQL's `User.email` field is scope-gated, and a token without
> it fails the *whole* query — but only once it meets a user who actually has an address
> set, so a scopeless token dies partway through a crawl rather than up front. gitscout
> detects that and carries on without profile emails (commit emails are unaffected),
> but you lose the cheapest email source. `gitscout doctor` tells you if the scope is
> missing.

---

## Why GraphQL, and why that matters for cron

Two properties of the GraphQL API drive the whole design.

**1. The profile comes free with the interaction.** One query returns 100 stargazers
*with* their name, company, location, bio, website, follower count and public email.

| | REST | GraphQL |
|---|---|---|
| 100 stargazers + full profiles | 1 + 100 = **101 requests** | **~1 point** |
| Hourly budget per token | 5,000 requests | 5,000 points |

GitHub's [documented cost formula](https://docs.github.com/en/graphql/overview/rate-limits-and-node-limits-for-the-graphql-api)
is "requests needed per connection ÷ 100, rounded, minimum 1", so a 100-item page
costs about one point. That is roughly a **100x** improvement on the same work.

**2. Connections can be ordered newest-first.** `orderBy: {field: STARRED_AT,
direction: DESC}` means a scheduled run reads page 1, stops at the first interaction it
has already recorded, and costs ~2 points per repo. The REST stargazers endpoint only
paginates oldest-first, so incremental polling would mean walking to the last page
every single time. **This is what makes a frequent cron affordable.**

Commit-email discovery is batched for the same reason: one aliased query probes ~10
users at a time (`u0: user(login: $l0) {...}`), so 10 users cost ~1 point instead of
~40 REST requests.

---

## Commands

| Command | What it does |
|---|---|
| `gitscout scout [REPO...] [-t PROFILE]` | the main command: ingest → enrich → score → export |
| `gitscout watch -t PROFILE --every 6h` | run `scout --incremental` on a schedule, in-process |
| `gitscout doctor` | validate token, DB, and **every GraphQL query against live GitHub** |
| `gitscout discover --topic cloud-security` | find more repos worth watching; `--save` writes a profile |
| `gitscout targets-list [-t PROFILE]` | show shipped profiles, or one profile's repos |
| `gitscout ingest REPO... [--api rest]` | stage 1 only |
| `gitscout enrich [--scan-websites]` | email discovery for everyone collected so far |
| `gitscout export out.jsonl [--new-since T]` | write CSV or JSONL (format inferred from the extension) |
| `gitscout score` | recompute lead scores |
| `gitscout stats` | counts, email hit rate, and a breakdown by source |
| `gitscout db [--table T] [--sql Q] [--login U]` | look inside the database. **Read-only** |
| `gitscout runs [--json]` | audit log of every run, including scheduled ones |
| `gitscout suppress alice bob@x.com` | do-not-contact list, excluded from every export |
| `gitscout apify REPO...` | the Apify fallback (costs money — see below) |
| `gitscout run REPO...` | the original REST pipeline, kept as a fallback |

Global options: `--db path.db` (or `GITSCOUT_DB`), `-v` for debug logs.
`REPO` is `owner/name` or a github.com URL.

**The six signals** (`-k`): `stars`, `forks`, `issues`, `prs`, `discussions`,
`contribs`. `contribs` reads the target repo's default-branch commit history, so on a
repo with tens of thousands of commits the first backfill is the most expensive of the
six — use `--max` to bound it. Incremental runs are cheap for all six, because commit
history is newest-first by definition.

### Useful invocations

```bash
# first-time backfill, capped so it finishes; re-run to continue (it resumes)
gitscout scout -t cloud-security --max 2000 -o out/leads.csv

# what a cron job runs: only what is new, exported on its own
gitscout scout -t cloud-security --incremental --new-only \
  -o out/new-leads.csv -o out/new-leads.jsonl --only-with-email

# squeeze out more addresses
gitscout scout -t cloud-security --scan-websites

# just the high-intent signals from a huge repo
gitscout scout kubernetes-sigs/kubespray -k issues,prs,contribs

# contributors only -- logins AND their commit emails, in one pass
gitscout scout prowler-cloud/prowler -k contribs -o out/contributors.csv

# grow the target list
gitscout discover --topic cloud-security --min-stars 300 --save my-targets.toml
```

---

## Scheduling

All three options drive the same incremental code path. Pick one.

### GitHub Actions (free, no infrastructure)

[`.github/workflows/scout.yml`](.github/workflows/scout.yml) is ready to go:

1. Add a repo secret `GITSCOUT_GITHUB_TOKENS` = your PAT.
2. **Actions → Scout → Run workflow** once to check it.
3. It then runs twice daily and uploads `leads-<n>` artifacts.

Do **not** use the built-in `GITHUB_TOKEN`: in Actions it is capped at 1,000 GraphQL
points per repository and cannot read user profile emails.

The SQLite database (which holds the high-water marks) is kept in the Actions cache
between runs. A cache miss is safe — the run just re-backfills — but costs more points.
If you want stronger durability, commit the database to a data branch instead.

### Docker

```bash
docker compose up -d        # scheduler, restarts on failure
docker compose logs -f
docker compose run --rm gitscout gitscout stats
```

Leads land in `./out`. State lives in the `gitscout-data` volume — keep it, or runs
stop being incremental.

Verified on this machine: the image builds, runs as the non-root `scout` user, and
creates `/data/gitscout.db` on the volume. On **Windows Git Bash**, prefix `docker run`
with `MSYS_NO_PATHCONV=1` when passing absolute container paths, or Git rewrites
`/data/...` into `C:/Program Files/Git/data/...`.

### Windows Task Scheduler

```powershell
$py = (Get-Command python).Source
schtasks /Create /TN "gitscout" /SC HOURLY /MO 6 /F `
  /TR "$py -m gitscout.cli scout -t cloud-security --incremental --new-only -o C:\gitscout\out\new-leads.csv"
```

Set `GITSCOUT_DB` to an absolute path so every run uses the same database.

---

## How emails are found

Cheapest and most reliable source first.

| # | Source | Confidence | Cost | Notes |
|---|---|---|---|---|
| 1 | `gql_profile` — the email on their GitHub profile | 0.95 | **free** | arrives with the interaction |
| 2 | `gql_contrib` — git author line of their commits **in the target repo** | 0.85 | **free** | arrives with the `contribs` signal |
| 3 | `gql_commit` — author line of commits in *their own* repos | 0.85 | ~0.1 pt/user | 0.42 when only the git *name* matches |
| 4 | `website` — the site linked from their profile (`--scan-websites`) | 0.60 | no GitHub quota | opt-in |
| 5 | `hunter` — Hunter.io email-finder (needs `HUNTER_API_KEY`) | ≤0.70 | paid | off by default |

`gql_contrib` is the best deal in the tool: reading a target repo's commit history
returns the contributor's login, full profile **and** their git address in a single
query, with attribution GitHub itself made (`author.user`). The REST `/contributors`
endpoint caps at 500 people and carries no email at all.

**Commit attribution is deliberate.** The probe does *not* use
`history(author: {id: ...})`, because that filter only matches commits GitHub has
already linked to the account — excluding exactly the unlinked commits whose addresses
we most want. Instead it over-fetches slightly and attributes client-side:

* `author.user.login` matches the account → full confidence
* no linked user but the git author *name* plausibly matches → half confidence
* anything else → **dropped** (a co-author or a merged contribution, not their address)

Discarded everywhere: `noreply.github.com`, `noreply@`/`no-reply@`/`bounce@`, example
and `.local`/`.invalid` domains, and `logo@2x.png`-style false positives.

### Expect 30–50%, not 90%

Only a minority of developers publish a profile email; commit emails add a lot more,
but anyone who enabled "keep my email private" is rewritten to `noreply.github.com` and
is simply unreachable. Active contributors resolve far better than passive stargazers.

`gitscout stats` reports your **actual** hit rate and a breakdown by source — trust that
over any vendor's advertised number.

---

## Lead scoring

Every user gets a transparent 0–100 score ([`scoring.py`](src/gitscout/scoring.py)), and
exports are ordered by it. The dominant term is *what they did*:

```
contribs 32 > prs 30 > issues 26 > discussions 22 > forks 18 > stars 10
  + multiple repos touched      (max 8)
  + followers, log-scaled       (max 15)
  + ICP keywords in bio/company (max 18, minus 8 for "student"/"bootcamp")
  + recency of the interaction  (max 15)
  + has a company               (6)
```

A deliberate weighted sum, not a model, so you can always explain a score. Once you
have reply/bounce labels, replace it — the interface is just
`score_rows(rows) -> {login: score}`.

---

## The Apify fallback

You asked whether Apify actors could do this. They can, and the adapter is included
(`gitscout apify`, configure with `APIFY_TOKEN` / `APIFY_ACTOR`) — but **the GraphQL
path is better for this job in every dimension that matters:**

| | GraphQL | Apify actor |
|---|---|---|
| Cost | free within 5k points/hr | ~$15 per 1,000 results, forever |
| Incremental runs | yes, newest-first + early stop | no — re-scrapes everything each run |
| Stability | versioned public API | scrapes HTML; breaks on markup changes |
| Provenance | per-address source + confidence | opaque |
| `starredAt` timestamps | yes | generally not |

The lack of an incremental mode is what disqualifies it for your cron requirement.
Use it when you genuinely cannot hold a GitHub token, or when you specifically want an
actor's extra website-scraped addresses.

Actors known to fit this adapter (set `APIFY_ACTOR` to the slug on the actor's page):

* `aleloro_dev~github-stars-email-extractor` — stargazers with emails (the default)
* `dtrungtin~github-users-scraper` — watchers/stargazers/members
* *GitHub Scraper* by `scrapesage`
* *GitHub Profile Scraper & Lead Finder* by `apivault_labs`

Because actors disagree on nearly every field name, the mapper in
[`apify.py`](src/gitscout/apify.py) is tolerant (`login`/`username`/`userName`/…) and
everything it imports is recorded at a modest `0.55` confidence, below any
GitHub-sourced address.

### Other enrichment tools

The provider interface ([`providers/`](src/gitscout/providers/)) takes anything with
`name`, `enabled` and `find(profile)`. A working **Hunter.io** adapter is included and
activates only when `HUNTER_API_KEY` is set. It asks only about people GitHub could not
resolve, and only when it has both a full name and a real company domain — free-mail
and platform hosts (gmail, github.io, vercel.app, medium…) are refused so you don't
burn credits. Apollo, Dropcontact, Prospeo and FindyMail would drop in the same way.
For cold outreach, add a verification pass (MillionVerifier, ZeroBounce) before sending.

---

## Rate limits and budget

* **GraphQL:** 5,000 points/hour per token. Comma-separate tokens in `GITHUB_TOKENS`
  to multiply it; they rotate automatically.
* Ingest costs ~1 point per 100 users (profiles included). Commit probing costs
  ~1 point per 10 users. So roughly **100k+ users/hour/token** for ingest.
* When every token is spent, the client sleeps until the reset and resumes.
* Everything is **resumable and idempotent**: re-run the same command.

```bash
gitscout rate-limit     # REST quota and GraphQL points per token
```

---

## Design notes

```
src/gitscout/
  graphql.py        async GraphQL client: TokenPool, point accounting, errors-inside-200,
                    cursor pagination that shrinks pages on node limits
  queries.py        every GraphQL document, in one file (doctor validates them all)
  ingest_gql.py     stars/forks/issues/PRs, newest-first, incremental early-stop
  enrich_gql.py     batched commit-email probes + client-side attribution
  scoring.py        the 0-100 lead score
  targets.py        TOML target profiles + GraphQL repo discovery
  scheduler.py      the watch loop; interval parsing; crash tolerance
  doctor.py         preflight that runs every query against live GitHub
  apify.py          the third-party fallback
  providers/        optional paid email providers (off unless keyed)
  storage.py        SQLite: interactions -> users -> emails, crawl_state, runs
  export.py         CSV (formula-injection hardened) and JSONL
  web.py            third-party site fetching, SSRF-guarded
  github_client.py  the original REST client, kept for `--api rest`
  ingest.py/enrich.py  the original REST stages
```

* **Incremental state:** `crawl_state` holds a cursor (resume a backfill) and a
  high-water mark (stop an incremental run). The high-water mark only ever moves
  forward, so an interrupted run can never cause a later run to skip records.
* **Safety:** GitHub tokens go only to `api.github.com`. Website scans use a separate
  client with no credentials, refuse private/loopback addresses (including on
  redirects), and cap response size. Commit-probe logins are passed as GraphQL
  *variables*, never interpolated into a query document.
* **CSV hardening:** bios and names are attacker-controlled, so cells starting with
  `= + - @` are prefixed with `'`. JSONL is left verbatim — it is not a spreadsheet.
* **Not people:** bots, organizations and mannequins are recorded as signals (a company
  forking your repo is interesting) but never appear in a lead export. `dependabot[bot]`
  and friends appear as *User*-typed nodes in commit history, so logins ending `[bot]`
  are filtered by name as well as by type.
* **Audit log:** every run records what it found and what it cost, so a silent
  scheduled failure is visible in `gitscout runs`.

### Looking at the data

The SQLite file is the source of truth; CSV/JSONL are just exports of it.

```bash
gitscout db                                  # every table + row count
gitscout db --table users -n 20              # dump a table
gitscout db --login alice                    # every address found for one person
gitscout db --sql "SELECT source, COUNT(*) FROM emails GROUP BY source"
gitscout db --table interactions -o raw.csv  # any of the above to a file
```

`gitscout db` is read-only twice over: non-read statements are rejected, *and* the
query runs on a connection SQLite itself refuses to write through — so a valid-but-
writing statement like `WITH x AS (...) DELETE FROM users` fails at the engine.

In VS Code, the **SQLite Viewer** extension opens `gitscout.db` with a click.

### Tests

```bash
python -m pytest -q        # 329 tests, fully mocked: no network, no token needed
```

Covers the GraphQL error matrix (NOT_FOUND / RATE_LIMITED / MAX_NODE_LIMIT_EXCEEDED /
502 / retryable timeouts / missing `read:user` scope), incremental stop semantics,
commit attribution rules,
batching and its node-limit fallback, the Apify mapper, provider gating, the watch
loop's crash tolerance, a v0.1 → v0.2 database migration, and a full pipeline
end-to-end.

---

## Responsible use

Everything collected is public, but using it is still regulated.

* GitHub's Terms of Service prohibit using information gathered from GitHub for spam or
  unsolicited bulk solicitation.
* Cold outreach is covered by CAN-SPAM (US), GDPR/PECR (EU/UK) and India's DPDP Act.
  A GitHub profile email is not consent to marketing.
* Keep it targeted and relevant, identify yourself, include a working unsubscribe, and
  add anyone who opts out with `gitscout suppress`.
* `--scan-websites` can surface addresses people did not intend to publish. It is
  opt-in for that reason.

## Upgrading from 0.1

`gitscout run` and the REST path still work unchanged. New databases are migrated in
place on first open (new columns and tables are added; nothing is dropped).
`requires-python` moved to **3.11+** because target profiles use `tomllib`.
