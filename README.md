# gitscout

Find the people who fork, file issues, open PRs, post discussions or land commits on
GitHub repos you care about — then discover their public emails. Exports to CSV.

Proprietary software. See [LICENSE](LICENSE).

---

## Setup (once)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
echo 'GITHUB_TOKENS=ghp_xxx' > .env                 # needs the read:user scope
gitscout doctor                                      # verifies token, DB, live queries
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
