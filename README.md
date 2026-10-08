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
 open .env            # then edit: GITHUB_TOKENS=ghp_...

# 3. prove it works against live GitHub
gitscout doctor

# 4. collect
gitscout scout --targets cloud-security --max 1000 -o out/leads.csv --only-with-email
```

> **A token is mandatory.** GitHub returns `401 Unauthorized` for anonymous API
> requests. Create one at <https://github.com/settings/tokens> and **tick `read:user`**
> — without that scope you lose profile emails entirely. One token = 5,000 GraphQL
> points/hour; comma-separate several in `GITHUB_TOKENS` and they rotate automatically.
> `gitscout doctor` tells you if anything is missing.

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
| `gitscout rate-limit` | REST quota and GraphQL points per token |
| `gitscout suppress alice bob@x.com` | do-not-contact list, excluded from every export |
| `gitscout run REPO...` | the original REST pipeline, kept as a fallback |

Global options: `--db path.db` (or `GITSCOUT_DB`), `-v` for debug logs.
`REPO` is `owner/name` or a github.com URL.

**The six signals** (`-k`): `stars`, `forks`, `issues`, `prs`, `discussions`,
`contribs`. `contribs` reads the target repo's commit history, so the first backfill on
a large repo is the most expensive of the six — bound it with `--max`.

```bash
# first-time backfill, capped so it finishes; re-run to continue (it resumes)
gitscout scout -t cloud-security --max 2000 -o out/leads.csv

# what a cron job runs: only what is new, exported on its own
gitscout scout -t cloud-security --incremental --new-only \
  -o out/new-leads.csv -o out/new-leads.jsonl --only-with-email
```

---

## Scheduling

All three options drive the same incremental code path. Pick one.

**GitHub Actions** — [`.github/workflows/scout.yml`](.github/workflows/scout.yml) is
ready to go: add a repo secret `GITSCOUT_GITHUB_TOKENS` = your PAT, run it once from
**Actions → Scout → Run workflow**, and it then runs twice daily and uploads
`leads-<n>` artifacts. Use a PAT, not the built-in `GITHUB_TOKEN`.

**Docker**

```bash
docker compose up -d        # scheduler, restarts on failure
docker compose logs -f
docker compose run --rm gitscout gitscout stats
```

Leads land in `./out`; state lives in the `gitscout-data` volume — keep it, or runs stop
being incremental.

**Windows Task Scheduler**

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

Every address carries its source and confidence. Addresses that cannot be attributed to
the person are dropped rather than guessed, and `noreply.github.com` and friends are
discarded everywhere.

**Expect 30–50%, not 90%.** Only a minority of developers publish an email, and anyone
who enabled "keep my email private" is unreachable. Active contributors resolve far
better than passive stargazers. `gitscout stats` reports your **actual** hit rate.

---

## Lead scoring

Every user gets a transparent 0–100 score ([`scoring.py`](src/gitscout/scoring.py)) and
exports are ordered by it. The dominant term is *what they did*
(`contribs > prs > issues > discussions > forks > stars`), adjusted for repos touched,
followers, ICP keywords, recency and whether they list a company. It is a deliberate
weighted sum, not a model, so a score can always be explained.

---

## Rate limits

5,000 GraphQL points/hour per token. Ingest costs ~1 point per 100 users (profiles
included); commit probing ~1 point per 10 users — roughly **100k+ users/hour/token**.
When every token is spent the client sleeps until the reset and resumes. Everything is
resumable and idempotent, so re-running the same command is always safe.

## Tests

```bash
python -m pytest -q        # 329 tests, fully mocked: no network, no token needed
```

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
