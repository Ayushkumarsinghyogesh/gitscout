# gitscout

Find the people who fork, file issues, open PRs, post discussions or land commits on
GitHub repos you care about — then discover their public emails. Exports to CSV.

Proprietary software. See [LICENSE](LICENSE).

---

## Setup (once)

```bash
# 1. Install (once)
sudo apt install -y git python3-venv
git clone https://github.com/Defendermate/gtm-eng.git
cd gtm-eng/gitscout
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
mkdir -p logs

# 2. Credentials (once; .env is git-ignored and stays on this machine)
echo 'GITHUB_TOKENS=ghp_your_token_here' > .env
python set-supabase-dsn.py          # prompts for the gitscout_writer password, writes SUPABASE_DB_URL

# 3. Health check
gitscout doctor                     # token, database and Supabase must all show OK

# 4. First run: at most 2000 records per repository per section, at most 2000 people enriched
gitscout scout -t trivy --max 2000 --limit 2000 --with-threads --with-social --push

# 5. Daily, automatic (09:07 and 21:07 server time). Paste this line into `crontab -e`:
7 9,21 * * * cd /home/USER/gtm-eng/gitscout && .venv/bin/gitscout scout -t cloud-security --incremental --max 2000 --limit 2000 --with-threads --with-social --push >> logs/sync.log 2>&1                                     # verifies token, DB, live queries
```

A token is **mandatory** — GitHub returns `401` without one.
Get one at <https://github.com/settings/tokens> and tick **`read:user`**, or you lose
profile emails entirely.

---

## The only command you need

```bash
gitscout scout -t trivy --max 500 -o out/leads.csv
```

That's it. This is 90% of all usage.

---

## Running it for DefenderMate (the daily job)

The production run crawls the repositories in the `cloud-security` profile (issues, pull
requests, discussions, forks, commits), collects the full threads, finds public emails
and LinkedIn links, pushes everything to the DefenderMate Supabase project and refreshes
the outreach dataset `"Defendermate2".final_outreach_users`.

You need two things before you start: a GitHub personal access token with the
`read:user` scope (<https://github.com/settings/tokens>) and the `gitscout_writer`
database password (ask the maintainer).

### Linux server, from zero

```bash
# 1. Install (once)
sudo apt install -y git python3-venv
git clone https://github.com/Defendermate/gtm-eng.git
cd gtm-eng/gitscout
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
mkdir -p logs

# 2. Credentials (once; .env is git-ignored and never leaves this machine)
echo 'GITHUB_TOKENS=ghp_your_token_here' > .env
python set-supabase-dsn.py          # asks for the gitscout_writer password, writes SUPABASE_DB_URL

# 3. Health check: token, database and Supabase must all show OK
gitscout doctor

# 4. First run: at most 2000 records per repository per section, at most 2000 people enriched
gitscout scout -t cloud-security --max 2000 --limit 2000 --with-threads --with-social --push
```

Then make it run by itself, twice a day (09:07 and 21:07 server time). Open the schedule
with `crontab -e` and paste this one line, with `/home/USER` replaced by the real path:

```
7 9,21 * * * cd /home/USER/gtm-eng/gitscout && .venv/bin/gitscout scout -t cloud-security --incremental --max 2000 --limit 2000 --with-threads --with-social --push >> logs/sync.log 2>&1
```

`crontab -l` shows it is registered; `tail -f logs/sync.log` shows a run. No manual
trigger is needed after this.

Standalone copy instead of the monorepo: `git clone https://github.com/Ayush-appagentix/gitscout.git && cd gitscout`.

### Windows

Same steps 1 to 4 in PowerShell (`.venv\Scripts\activate` instead of `source`), then
register the scheduled task, which runs at 09:07 and 21:07 with a hidden window and
catches up on runs missed while the PC was off:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1
Get-ScheduledTaskInfo -TaskName gitscout-scheduled-sync | fl LastRunTime, LastTaskResult, NextRunTime
```

The task runs only while the user who registered it is logged in (a locked screen is fine).

### Docker

`docker compose up -d` runs only the CSV-export loop (`gitscout watch`), **not** the
Supabase push. To run the daily job inside the container, put this in cron instead of
the line above:

```
7 9,21 * * * cd /home/USER/gtm-eng/gitscout && docker compose run --rm gitscout gitscout scout -t cloud-security --incremental --max 2000 --limit 2000 --with-threads --with-social --push >> logs/sync.log 2>&1
```

### What to expect

- **Incremental and resumable.** Cursors are saved after every page. A run that stops,
  or hits the 2000 cap, is continued by the next one; over a few runs everything is
  covered. `--max` is per repository per section, `--limit` is people enriched per run.
- **Idempotent and locked.** Running it twice changes nothing; two runs never overlap.
- **Cost.** A full backfill of trivy is about 1,600 GitHub API points of the 5,000 per
  hour a token gets; the daily incremental run is a few dozen. If the budget runs out
  mid-run the client waits for the reset and continues.
- **Time.** First run on trivy with the 2000 caps: 10 to 20 minutes. Daily run: 1 to 5
  minutes, plus about 20 seconds to refresh the dataset.

Useful afterwards: `gitscout stats` (people and emails found), `gitscout rate-limit`
(GitHub quota left), `gitscout doctor` whenever something fails. Pipeline details and
costs: [docs/ingestion-and-scheduling.md](docs/ingestion-and-scheduling.md). The dataset
and every column: [docs/outreach-data-model.md](docs/outreach-data-model.md).

---

## The mental model

Everything is one pipeline of four stages:

```
ingest   ->   enrich   ->   score   ->   export
(who)        (emails)      (rank)       (csv)
```

**`scout` runs all four.** Every other command is either one stage on its own, or a way
to look at the results. That's the whole tool.

---

## Three flags to remember

| Flag | Why you need it |
|---|---|
| `--max N` | cap the fetch — without it, a big repo runs for hours |
| `--limit N` | cap the enrich stage — this is the slow one |
| `-o file.csv` | where the output goes (`.jsonl` also works) |

```bash
gitscout scout -t cloud-security --max 500 --limit 300 -o out/leads.csv
```

---

## Everything else, grouped

**One stage at a time** — same four names as the pipeline above
`ingest` · `enrich` · `score` · `export`

**Look, don't touch** — safe, read-only
`stats` · `db` · `runs` · `rate-limit` · `targets-list` · `doctor`

**Occasional**
`watch` (run on a schedule) · `discover` (find new repos to watch) ·
`suppress` (do-not-contact list) · `threads` + `thread-tails` (full issue/PR/discussion
records, not just "who touched what") · `supabase push` (ship rows to Supabase)

**Legacy fallbacks** — you probably never need these
`apify` (paid third-party scraper) · `run` (the old REST pipeline)

---

## Four things that will trip you up

**1. A repo is positional. A profile needs `-t`.**

```bash
gitscout scout prowler-cloud/prowler     # repo — no flag
gitscout scout -t cloud-security         # profile — with -t
```

**2. `stars` returns nothing.** GitHub restricted the stargazer *list* to repo
admins/collaborators on 2026-06-30. The star count is still public, the list is not.
It only works on repos you administer, so it is excluded from the default signals.

**3. `-k` is ignored when you pass `-t`.** A profile sets its signals per repo in the
TOML. `-k` only applies to repos you name on the command line.

**4. `enrich` is the slow stage.** It probes commits for everyone who has no email yet,
so it grows with your database. Always bound it with `--limit`. It is resumable — a
killed run picks up where it left off.

---

## When something breaks

```bash
gitscout doctor      # start here, always
gitscout stats       # how many leads, how many emails, by source
gitscout --help      # every command
gitscout scout --help   # every flag, with its default
```

Don't memorise flags — `--help` prints the defaults too.
