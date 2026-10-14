"""Push the local store into a Supabase (Postgres) mirror, over PostgREST.

SQLite stays the source of truth: it holds the crawl cursors and high-water marks, and
it is what the pipeline writes to. This module copies the *results* somewhere a team
can query them -- a dashboard, a CRM sync, an outreach tool.

Why PostgREST and not psycopg: `httpx` is already a dependency and the REST endpoint
needs nothing but a URL and a key, so the sync adds **no new dependency** and works
from anywhere outbound HTTPS works (a CI runner, a container, a laptop on a hotel
network). Upserts go through `Prefer: resolution=merge-duplicates`, which is a real
`ON CONFLICT DO UPDATE` server-side, so re-running a sync is idempotent.

Three things are deliberate:

* **Incremental by default.** Each table has a column that changes when *gitscout*
  touches a row (`users.fetched_at`, `emails.found_at`, `threads.fetched_at`,
  `thread_comments.changed_at`) -- not GitHub's own `updated_at`, which would make a
  backfill of old threads look unchanged and skip them. The watermark lives in
  `crawl_state` under `supabase:sync`, so it survives a crash and only moves forward.
* **Parents before children, and never an orphan.** The remote schema has real foreign
  keys, so leads are pushed before their emails and threads before their comments. A
  child row whose parent is not in this batch (a new address for a profile that has not
  been refetched) would violate the key, so those parents are fetched and included.
* **The service key never leaves this process.** It goes in an `Authorization` header
  to the configured Supabase host and nowhere else, and it is redacted from errors.
  Use the service_role key, not the anon key: the tables have RLS on precisely so the
  anon key cannot read other people's email addresses.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import httpx

from .config import Settings
from .storage import Store

log = logging.getLogger(__name__)

#: Rows per request. PostgREST handles far more, but a failed batch is re-sent whole,
#: so this trades a little throughput for a cheaper retry.
DEFAULT_BATCH = 500

#: Where the push watermark lives in `crawl_state`.
SYNC_KEY = "supabase:sync"

#: Local table -> (remote table, conflict target). The conflict target must match the
#: remote primary key or PostgREST cannot turn the insert into an upsert.
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("repositories", "repositories", "repo"),
    ("people", "people", "login"),
    ("person_emails", "person_emails", "login,email,source"),
    ("threads", "threads", "repo,kind,number"),
    ("comments", "comments", "repo,kind,number,comment_id"),
    ("interactions", "interactions", "repo,login,kind"),
)

#: Where the mirror lives. A schema of its own, so the host project's `public` stays
#: exactly as its own migrations describe it.
DEFAULT_SCHEMA = "gitscout"

#: Columns the remote schema does not have, dropped before sending. PostgREST rejects
#: an unknown column outright rather than ignoring it.
_DROP = {
    "people": ("_changed_at",),   # local bookkeeping for the watermark
    "threads": ("added_at",),
    "comments": ("added_at", "changed_at"),
}

#: Local column -> mirror column, where the two differ.
_RENAME = {
    "threads": {"fetched_at": "github_fetched_at"},
    "repositories": {"fetched_at": "github_fetched_at"},
    "interactions": {"added_at": "first_recorded_at"},
}


class SupabaseError(RuntimeError):
    """A push failed. The message never contains the key."""


@dataclass
class SyncResult:
    mode: str = "incremental"
    since: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    seconds: float = 0.0
    watermark: str | None = None
    dry_run: bool = False
    refreshed: bool = False
    outreach_rows: int | None = None
    outreach_views: list[str] = field(default_factory=list)
    refresh_error: str | None = None

    @property
    def rows(self) -> int:
        return sum(self.counts.values())


def _batched(rows: Sequence[Mapping[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield [dict(r) for r in rows[start : start + size]]


def _prepare(name: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Strip local-only columns and rename the ones the mirror spells differently.

    `sync_leads` already aliases its columns to the mirror's names, so people need no
    translation here -- only the two thread tables carry bookkeeping that is ours.
    """
    drop = _DROP.get(name, ())
    rename = _RENAME.get(name, {})
    return [
        {rename.get(k, k): v for k, v in row.items() if k not in drop} for row in rows
    ]


class SupabaseClient:
    """A very small PostgREST client: upsert, count, and nothing else."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        schema: str = "public",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url or not key:
            raise SupabaseError(
                "Supabase is not configured: set SUPABASE_URL and SUPABASE_SERVICE_KEY "
                "(the service_role key, not the anon key). See `gitscout doctor`."
            )
        self.base = url.rstrip("/")
        if not self.base.endswith("/rest/v1"):
            self.base += "/rest/v1"
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Content-Profile": schema,
            "Accept-Profile": schema,
        }
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.requests = 0

    async def __aenter__(self) -> "SupabaseClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self, method: str, path: str, *, headers: Mapping[str, str] | None = None, **kw: Any
    ) -> httpx.Response:
        """One request, retried on the failures that are worth retrying.

        ``headers`` is merged over the client's own, so a caller adds `Prefer`
        without having to repeat the auth headers.
        """
        merged = {**self._headers, **(headers or {})}
        last: Exception | None = None
        for attempt in range(4):
            try:
                self.requests += 1
                response = await self._client.request(
                    method, f"{self.base}/{path}", headers=merged, **kw
                )
            except httpx.HTTPError as exc:  # network-level
                last = exc
                await self._backoff(attempt)
                continue
            if response.status_code < 300:
                return response
            if response.status_code in (429, 500, 502, 503, 504):
                last = SupabaseError(f"HTTP {response.status_code}")
                await self._backoff(attempt, response)
                continue
            raise SupabaseError(self._explain(response))
        raise SupabaseError(f"gave up after 4 attempts: {last}")

    @staticmethod
    async def _backoff(attempt: int, response: httpx.Response | None = None) -> None:
        """Honour Retry-After when the server sends one, else exponential."""
        delay = 0.0
        if response is not None:
            try:
                delay = float(response.headers.get("retry-after") or 0)
            except ValueError:
                delay = 0.0
        await asyncio.sleep(delay or min(2**attempt, 8))

    @staticmethod
    def _explain(response: httpx.Response) -> str:
        """A readable error. PostgREST puts the useful part in a JSON body."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = " ".join(
            str(body[k]) for k in ("message", "details", "hint", "code") if body.get(k)
        )
        text = detail or (response.text or "")[:300]
        if "does not exist" in text or response.status_code == 404:
            text += (
                "  -- the mirror tables are missing. Run `gitscout supabase schema` and "
                "paste the output into the Supabase SQL editor."
            )
        return f"HTTP {response.status_code}: {text}"

    async def upsert(
        self, table: str, rows: Sequence[Mapping[str, Any]], *, on_conflict: str, batch: int
    ) -> int:
        """Insert-or-update rows. Returns how many were sent."""
        sent = 0
        for chunk in _batched(rows, batch):
            await self._send(
                "POST",
                f"{table}?on_conflict={on_conflict}",
                json=chunk,
                # merge-duplicates = ON CONFLICT DO UPDATE; minimal = do not ship the
                # rows back, which halves the traffic on a big push.
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            sent += len(chunk)
            log.info("supabase: %s +%d/%d", table, sent, len(rows))
        return sent

    async def count(self, table: str) -> int:
        response = await self._send(
            "GET", f"{table}?select=*", headers={"Prefer": "count=exact"}
        )
        content_range = response.headers.get("content-range", "")
        return int(content_range.rsplit("/", 1)[-1]) if "/" in content_range else 0


class PostgresWriter:
    """Upsert straight into Postgres, for a project whose Data API is not exposed.

    Supabase's PostgREST is optional: a project can keep "Exposed schemas" empty so
    nothing is reachable over HTTP, and talk to the database only from trusted code.
    This writer is that path. It changes nothing about the project's exposure, which
    is why it is preferred over asking someone to switch PostgREST on for one sync.

    Rows go in as chunked multi-row INSERT ... ON CONFLICT DO UPDATE. COPY would be
    faster, but it cannot express the upsert and these volumes are tens of thousands
    of rows, not millions -- the whole mirror lands in a handful of seconds.

    ``psycopg`` is an optional dependency (``pip install "gitscout[postgres]"``): the
    REST path, which most installs use, needs nothing extra.
    """

    def __init__(self, dsn: str, *, batch: int = DEFAULT_BATCH, connect_timeout: int = 15) -> None:
        if not dsn:
            raise SupabaseError(
                "no Postgres DSN: set SUPABASE_DB_URL (Supabase -> Connect -> "
                "Connection string -> Transaction pooler)"
            )
        try:
            import psycopg  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise SupabaseError(
                'psycopg is not installed. Run: pip install "gitscout[postgres]"'
            ) from exc
        self._dsn = dsn
        self.batch = batch
        self.connect_timeout = connect_timeout
        self._conn: Any = None
        self.statements = 0

    def __enter__(self) -> "PostgresWriter":
        import psycopg

        try:
            self._conn = psycopg.connect(
                self._dsn,
                connect_timeout=self.connect_timeout,
                autocommit=False,
                # Supabase's transaction pooler is pgbouncer, which hands a different
                # backend to each transaction. psycopg prepares a statement after the
                # fifth execution and the next transaction then fails with
                # "prepared statement ... does not exist". Disabling prepares is the
                # documented way to use a transaction pooler, and costs nothing here:
                # each chunk is a distinct statement anyway.
                prepare_threshold=None,
            )
        except Exception as exc:  # noqa: BLE001
            raise SupabaseError(f"could not connect: {_redact(str(exc))}") from exc
        return self

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        if self._conn is None:
            return
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
            self._conn = None

    def upsert(self, table: str, rows: Sequence[Mapping[str, Any]], *, on_conflict: str) -> int:
        """Insert-or-update rows. Returns how many were sent."""
        if not rows:
            return 0
        if on_conflict == "id":
            # The audit log is append-only: `id` is a serial the payload never carries,
            # so there is nothing to conflict on.
            return self._insert(table, rows)
        columns = list(rows[0].keys())
        keys = [k.strip() for k in on_conflict.split(",")]
        updatable = [c for c in columns if c not in keys]
        assignments = ", ".join(f"{c} = excluded.{c}" for c in updatable)
        # synced_at has a default, so it is absent from the payload and would keep its
        # original value on an update. Refresh it explicitly.
        assignments += ", synced_at = now()" if assignments else "synced_at = now()"

        placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
        head = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
        )
        tail = f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"

        sent = 0
        with self._conn.cursor() as cur:
            for start in range(0, len(rows), self.batch):
                chunk = rows[start : start + self.batch]
                values: list[Any] = []
                for row in chunk:
                    values.extend(_adapt(row.get(c)) for c in columns)
                sql = head + ", ".join([placeholders] * len(chunk)) + tail
                try:
                    cur.execute(sql, values)
                except Exception as exc:  # noqa: BLE001
                    raise SupabaseError(f"{table}: {_redact(str(exc))}") from exc
                self.statements += 1
                sent += len(chunk)
                log.info("supabase(pg): %s +%d/%d", table, sent, len(rows))
        return sent

    def _insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        columns = list(rows[0].keys())
        placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
        with self._conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders}",
                    [_adapt(row.get(c)) for c in columns],
                )
                self.statements += 1
        return len(rows)

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one statement and return the first column of its first row."""
        with self._conn.cursor() as cur:
            try:
                cur.execute(sql, list(params))
            except Exception as exc:  # noqa: BLE001
                raise SupabaseError(_redact(str(exc))) from exc
            self.statements += 1
            row = cur.fetchone()
            return row[0] if row else None

    def count(self, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return int(cur.fetchone()[0])


def _adapt(value: Any) -> Any:
    """psycopg adapts lists to arrays but not dicts to jsonb -- wrap those explicitly.

    `activity_hours_utc` is the only dict-valued column; without this the direct path
    fails on its first person. Lists of dicts would be jsonb too, but no mirror column
    holds one.
    """
    if isinstance(value, dict):
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    return value


def _redact(message: str) -> str:
    """Connection errors echo the DSN, password and all."""
    return re.sub(r"://[^@\s]+@", "://***:***@", message)


def collect(
    store: Store, since: str | None, *, only_with_email: bool = False, limit: int = 0
) -> dict[str, list[dict[str, Any]]]:
    """Gather one push, parents first, with no orphaned child rows.

    A child whose parent is not in this batch is the one thing that can break the
    remote foreign keys, so the parents it needs are fetched and added back.
    """
    leads = store.sync_leads(since, only_with_email=only_with_email, limit=limit)
    emails = store.sync_emails(since, limit=limit)
    threads = store.sync_threads(since, limit=limit)
    comments = store.sync_thread_comments(since, limit=limit)

    if only_with_email:
        keep = {person["login"] for person in leads}
        emails = [e for e in emails if e["login"] in keep]

    known = {lead["login"] for lead in leads}
    missing = {e["login"] for e in emails} - known
    if missing:
        extra = store.sync_leads(None, only_with_email=only_with_email)
        leads.extend(lead for lead in extra if lead["login"] in missing)
        known |= {lead["login"] for lead in leads}
    # An email whose lead is filtered out entirely (a bot, or suppressed) has nowhere
    # to hang, so it is dropped rather than sent to fail the foreign key.
    emails = [e for e in emails if e["login"] in known]

    thread_keys = {(t["repo"], t["kind"], t["number"]) for t in threads}
    orphans = {
        (c["repo"], c["kind"], c["number"]) for c in comments
    } - thread_keys
    if orphans:
        for row in store.sync_threads(None):
            if (row["repo"], row["kind"], row["number"]) in orphans:
                threads.append(row)
                thread_keys.add((row["repo"], row["kind"], row["number"]))
    comments = [
        c for c in comments if (c["repo"], c["kind"], c["number"]) in thread_keys
    ]

    return {
        # The registry is a handful of rows and every view joins it, so it is always
        # pushed whole rather than tracked by watermark.
        "repositories": store.sync_repositories(),
        "people": leads,
        "person_emails": emails,
        "threads": threads,
        "comments": comments,
        # Incremental like everything else. A newly mirrored table needs one
        # `--full` push to backfill it; a newly added repository does not, because
        # its rows are recorded after the watermark anyway.
        "interactions": store.sync_interactions(since),
    }


def watermark_of(payload: Mapping[str, Sequence[Mapping[str, Any]]]) -> str | None:
    """The newest local timestamp in this push, which becomes the next `since`."""
    stamps: list[str] = []
    for name, column in (
        ("people", "_changed_at"),
        ("person_emails", "found_at"),
        ("threads", "fetched_at"),
        ("comments", "changed_at"),
        ("interactions", "added_at"),
    ):
        for row in payload.get(name, ()):
            value = row.get(column) or row.get("added_at")
            if value:
                stamps.append(str(value))
    return max(stamps) if stamps else None


async def push(
    settings: Settings,
    store: Store,
    *,
    full: bool = False,
    only_with_email: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SyncResult:
    """Push the store into Supabase. Idempotent: re-running it changes nothing new."""
    started = time.monotonic()
    since = None if full else (store.get_crawl_state(SYNC_KEY) or {}).get("high_water")
    result = SyncResult(
        mode="full" if full else "incremental", since=since, dry_run=dry_run
    )

    if settings.supabase_transport == "postgres" and not dry_run:
        _push_postgres(
            settings, store, result,
            full=full, since=since, only_with_email=only_with_email, limit=limit,
        )
        result.seconds = round(time.monotonic() - started, 2)
        return result

    payload = collect(store, since, only_with_email=only_with_email, limit=limit)
    result.counts = {name: len(rows) for name, rows in payload.items()}
    result.watermark = watermark_of(payload)

    if dry_run:
        result.seconds = round(time.monotonic() - started, 2)
        return result
    if not result.rows:
        log.info("supabase: nothing new to push")
        result.seconds = round(time.monotonic() - started, 2)
        return result

    async with SupabaseClient(
        settings.supabase_url or "",
        settings.supabase_key or "",
        schema=settings.supabase_schema,
        transport=transport,
    ) as client:
        try:
            for name, table, conflict in TARGETS:
                rows = _prepare(name, payload[name])
                if rows:
                    await client.upsert(
                        table, rows, on_conflict=conflict, batch=settings.supabase_batch
                    )
            result.seconds = round(time.monotonic() - started, 2)
            result.requests = client.requests
            await _log_run(client, result)
        except SupabaseError as exc:
            result.seconds = round(time.monotonic() - started, 2)
            result.requests = client.requests
            await _log_run(client, result, error=str(exc))
            raise

    # Only after every table landed: a partial push must not advance the watermark,
    # or the rows that failed would never be retried.
    if result.watermark:
        store.set_crawl_state(SYNC_KEY, high_water=result.watermark)
    return result


#: Set while the outreach dataset is behind the mirror. A refresh that fails leaves
#: it set, so the next run refreshes even if it brings no new data.
REFRESH_KEY = "supabase:refresh"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema(settings: Settings) -> str:
    schema = settings.supabase_schema or DEFAULT_SCHEMA
    if not _IDENTIFIER.match(schema):
        raise SupabaseError(f"SUPABASE_SCHEMA must be a plain identifier (got {schema!r})")
    return schema


def _readers(store: Store) -> dict[str, Any]:
    """Whole-table readers, for a full push that streams one table at a time."""
    return {
        "repositories": store.sync_repositories,
        "people": lambda: store.sync_leads(None),
        "person_emails": lambda: store.sync_emails(None),
        "threads": lambda: store.sync_threads(None),
        "comments": lambda: store.sync_thread_comments(None),
        "interactions": lambda: store.sync_interactions(None),
    }


def _push_postgres(
    settings: Settings,
    store: Store,
    result: SyncResult,
    *,
    full: bool,
    since: str | None,
    only_with_email: bool,
    limit: int,
) -> None:
    """The direct-Postgres push: the production path for scheduled runs.

    1. Every table lands in ONE transaction, parents before children, or none does.
    2. Only after that commits: per-repository views are ensured and the outreach
       dataset is refreshed, in a second transaction. A failed refresh never rolls
       back data that arrived correctly; it is remembered and retried next run.

    A full push streams one table at a time instead of holding the mirror in memory
    -- every parent is in the push by definition, so no orphan check is needed.
    """
    schema = _schema(settings)
    dsn = settings.supabase_db_url or ""
    streaming = full and not only_with_email and not limit
    marks: list[str] = []

    with PostgresWriter(dsn, batch=settings.supabase_batch) as db:
        if streaming:
            readers = _readers(store)
            for name, table, conflict in TARGETS:
                rows = readers[name]()
                result.counts[name] = len(rows)
                mark = watermark_of({name: rows})
                if mark:
                    marks.append(mark)
                if rows:
                    db.upsert(f"{schema}.{table}", _prepare(name, rows), on_conflict=conflict)
                del rows
        else:
            payload = collect(store, since, only_with_email=only_with_email, limit=limit)
            result.counts = {name: len(rows) for name, rows in payload.items()}
            mark = watermark_of(payload)
            if mark:
                marks.append(mark)
            for name, table, conflict in TARGETS:
                rows = _prepare(name, payload[name])
                if rows:
                    db.upsert(f"{schema}.{table}", rows, on_conflict=conflict)
        db.upsert(
            f"{schema}.sync_runs",
            [{
                "mode": result.mode,
                "since": result.since,
                "people": result.counts.get("people", 0),
                "person_emails": result.counts.get("person_emails", 0),
                "threads": result.counts.get("threads", 0),
                "comments": result.counts.get("comments", 0),
                "status": "ok",
            }],
            on_conflict="id",
        )
        result.requests = db.statements
    # ---- committed. Only now may the watermark move.
    result.watermark = max(marks) if marks else None
    if result.watermark:
        store.set_crawl_state(SYNC_KEY, high_water=result.watermark)

    # The registry is re-sent on every run by design, so it is not evidence of a
    # change; rebuilding the dataset because one unchanged row was upserted is load.
    changed = result.rows - result.counts.get("repositories", 0) > 0
    pending = (store.get_crawl_state(REFRESH_KEY) or {}).get("done") is False
    if not (changed or pending):
        log.info("supabase: no data changed; outreach dataset left as is")
        return

    store.set_crawl_state(REFRESH_KEY, done=False)
    try:
        with PostgresWriter(dsn) as db:
            for repo in store.sync_repositories():
                result.outreach_views.append(
                    db.scalar(f"select {schema}.ensure_outreach_view(%s)", [repo["repo"]])
                )
            result.outreach_rows = db.scalar(f"select {schema}.refresh_outreach()")
            result.requests += db.statements
        store.set_crawl_state(REFRESH_KEY, done=True)
        result.refreshed = True
    except SupabaseError as exc:
        # Data is safe and committed; the previous dataset keeps being served.
        result.refresh_error = str(exc)
        log.warning("supabase: outreach refresh failed, will retry next run: %s", exc)


async def _log_run(
    client: SupabaseClient, result: SyncResult, error: str | None = None
) -> None:
    """Best-effort audit row. A failed log must not fail the sync."""
    try:
        await client.upsert(
            "sync_runs",
            [
                {
                    "mode": result.mode,
                    "since": result.since,
                    "people": result.counts.get("people", 0),
                    "person_emails": result.counts.get("person_emails", 0),
                    "threads": result.counts.get("threads", 0),
                    "comments": result.counts.get("comments", 0),
                    "seconds": result.seconds,
                    "status": "failed" if error else "ok",
                    "error": error,
                }
            ],
            on_conflict="id",
            batch=1,
        )
    except SupabaseError as exc:  # noqa: BLE001
        log.warning("supabase: could not write the sync log: %s", exc)


def schema_sql() -> str:
    """The DDL for the mirror, as shipped inside the package."""
    from pathlib import Path

    path = Path(__file__).resolve().parent / "supabase_schema.sql"
    if not path.exists():
        raise SupabaseError(f"the bundled schema is missing: {path}")
    return path.read_text(encoding="utf-8")
