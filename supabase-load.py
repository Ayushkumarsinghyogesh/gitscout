"""Push the gitscout mirror through Supabase's Management API SQL endpoint.

Used when a project keeps its Data API switched off *and* the database password is
shared with other systems, so neither PostgREST nor a direct DSN is available. The
project's personal access token is the only credential, and it already works.

Rows travel as one dollar-quoted JSON document per batch and are expanded server side
with `jsonb_populate_recordset`, so nothing has to be SQL-escaped: a bio containing a
quote, a backslash or a semicolon is just JSON.
"""
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "src")
from gitscout.storage import Store                      # noqa: E402
from gitscout.supabase import SYNC_KEY, TARGETS, _prepare, collect, watermark_of  # noqa: E402

REF = "nqnocdkwdnvczxuvhcvr"
API = "https://api.supabase.com/v1"
ENV_PATHS = (
    r"C:\Users\Admin\Documents\Defendermate\.env",
    "/mnt/c/Users/Admin/Documents/Defendermate/.env",
)
#: Keep each statement well under the endpoint's payload ceiling.
MAX_SQL_BYTES = 400_000
TAG = "gsjson"
#: The mirror lives in its own schema, never in the host project's `public`.
SCHEMA = "gitscout"


def token():
    for path in ENV_PATHS:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip().startswith("SUPABASE_ACCESS_TOKEN="):
                        return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    sys.exit("SUPABASE_ACCESS_TOKEN not found")


TOKEN = token()


def sql(statement):
    req = urllib.request.Request(
        f"{API}/projects/{REF}/database/query",
        method="POST",
        data=json.dumps({"query": statement}).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else []
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"HTTP {e.code}: {body}")
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    return []


def batches(rows, columns):
    """Group rows so each statement's JSON stays under the payload ceiling."""
    current, size = [], 0
    for row in rows:
        blob = json.dumps({c: row.get(c) for c in columns}, ensure_ascii=False, default=str)
        if current and size + len(blob) > MAX_SQL_BYTES:
            yield current
            current, size = [], 0
        current.append(json.loads(blob))
        size += len(blob)
    if current:
        yield current


def upsert(table, rows, conflict):
    if not rows:
        return 0
    table = f"{SCHEMA}.{table}"
    columns = list(rows[0].keys())
    keys = [k.strip() for k in conflict.split(",")]
    sets = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
    sets = (sets + ", " if sets else "") + "synced_at = now()"
    cols = ", ".join(columns)

    sent = 0
    for chunk in batches(rows, columns):
        payload = json.dumps(chunk, ensure_ascii=False, default=str)
        if f"${TAG}$" in payload:
            raise SystemExit("payload contains the dollar-quote tag; pick another")
        statement = (
            f"insert into {table} ({cols}) select {cols} from "
            f"jsonb_populate_recordset(null::{table}, ${TAG}${payload}${TAG}$::jsonb) "
            f"on conflict ({', '.join(keys)}) do update set {sets}"
        )
        sql(statement)
        sent += len(chunk)
        print(f"  {table:28} {sent:>6}/{len(rows)}", flush=True)
    return sent


#: Rebuilt after every push so the outreach dataset reflects what just landed.
OUTREACH_VIEW = '"Defendermate2".final_outreach_users'


#: The one-off pg_cron job that performs the refresh.
REFRESH_JOB = "gitscout-outreach-refresh-once"


def refresh_outreach():
    """Queue a refresh of the outreach dataset inside the database.

    It cannot run through this endpoint: the Management API caps each statement at
    two minutes and the refresh outgrew that once PR threads were added. So it is
    handed to pg_cron -- already this project's scheduler -- as a job that runs once,
    within a minute, and removes itself.

    pg_cron runs a command as one transaction, so the self-removal lives inside
    gitscout.refresh_outreach_once(), which catches a failed refresh in a
    subtransaction: the job always removes itself, and the outcome (ok or the error)
    lands in gitscout.sync_runs. CONCURRENTLY keeps the previous dataset readable.
    """
    # Scheduling lives in the database (gitscout.queue_outreach_refresh): pinned to a
    # single minute, so it can never become a job that refires every minute. See
    # migrations/0004_refresh_cannot_loop.sql for the incident behind this.
    try:
        sql("select gitscout.queue_outreach_refresh()")
        print(f"queued {OUTREACH_VIEW} refresh as pg_cron job '{REFRESH_JOB}' (runs once)")
    except SystemExit as exc:
        print(f"WARNING: could not queue the outreach refresh, previous dataset still served: {exc}")


def main():
    full = "--full" in sys.argv
    dry = "--dry-run" in sys.argv
    started = time.monotonic()
    with Store("gitscout.db") as store:
        since = None if full else (store.get_crawl_state(SYNC_KEY) or {}).get("high_water")
        print(f"mode={'full' if full else 'incremental'} since={since} dry_run={dry}")
        if full and not dry:
            # Full mode streams one table at a time instead of holding the whole
            # mirror in memory -- an earlier all-at-once run was SIGKILLed. No
            # orphan check is needed: every parent is in the push by definition.
            readers = {
                "repositories": store.sync_repositories,
                "people": lambda: store.sync_leads(None),
                "person_emails": lambda: store.sync_emails(None),
                "threads": lambda: store.sync_threads(None),
                "comments": lambda: store.sync_thread_comments(None),
                "interactions": lambda: store.sync_interactions(None),
            }
            counts, mark = {}, None
            for name, table, conflict in TARGETS:
                rows = readers[name]()
                counts[name] = len(rows)
                mark = max(filter(None, [mark, watermark_of({name: rows})]), default=None)
                upsert(table, _prepare(name, rows), conflict)
                del rows
            seconds = round(time.monotonic() - started, 2)
            sql(
                f"insert into {SCHEMA}.sync_runs (mode, people, person_emails, threads, "
                "comments, seconds, status) values "
                f"('full', {counts['people']}, {counts['person_emails']}, "
                f"{counts['threads']}, {counts['comments']}, {seconds}, 'ok')"
            )
            if mark:
                store.set_crawl_state(SYNC_KEY, high_water=mark)
            print(f"\npushed {sum(counts.values())} rows in {seconds}s; watermark -> {mark}")
            refresh_outreach()
            return

        payload = collect(store, since)
        counts = {name: len(rows) for name, rows in payload.items()}
        print("to push:", counts, flush=True)

        if dry:
            # Prove the whole path works without touching the remote: build every
            # statement, check it is well formed and within the payload ceiling,
            # then throw it away.
            for name, table, conflict in TARGETS:
                rows = _prepare(name, payload[name])
                if not rows:
                    continue
                chunks = list(batches(rows, list(rows[0].keys())))
                biggest = max(
                    len(json.dumps(c, ensure_ascii=False, default=str)) for c in chunks
                )
                print(
                    f"  {table:28} {len(rows):>6} rows in {len(chunks):>3} statement(s), "
                    f"largest {biggest // 1024} KB"
                )
            print("\ndry run only, nothing was sent")
            return

        total = 0
        for name, table, conflict in TARGETS:
            total += upsert(table, _prepare(name, payload[name]), conflict)

        seconds = round(time.monotonic() - started, 2)
        sql(
            f"insert into {SCHEMA}.sync_runs (mode, people, person_emails, threads, "
            "comments, seconds, status) values "
            f"('{'full' if full else 'incremental'}', {counts['people']}, "
            f"{counts['person_emails']}, {counts['threads']}, {counts['comments']}, "
            f"{seconds}, 'ok')"
        )
        mark = watermark_of(payload)
        if mark:
            store.set_crawl_state(SYNC_KEY, high_water=mark)
        print(f"\npushed {total} rows in {seconds}s; watermark -> {mark}")
        # The registry is re-sent on every run by design, so it is not evidence of a
        # change. Rebuilding a 2-minute materialized view on production because one
        # unchanged row was upserted would be pure load.
        if total - counts.get("repositories", 0) > 0:
            refresh_outreach()
        else:
            print("no data changed; outreach dataset left as is")


if __name__ == "__main__":
    main()
