"""Durable job queue, run history and schedule evaluation, on Postgres.

The worker (which runs ingestion) and the dashboard (which asks for runs and shows
them) share this one module, so there is exactly one definition of what a job is.
State lives in ``gitscout.jobs`` and ``gitscout.repositories`` (migration 0011); nothing
is held in memory between calls, which is what lets a worker restart mid-run and carry
on.

Three properties the rest of the system relies on:

* **No overlap.** The database allows one queued-or-running job per (repository,
  kind) -- a partial unique index, not a check in Python -- so two workers, or a
  worker and a dashboard click, cannot start the same run twice. ``enqueue`` reports
  the refusal as ``None`` instead of raising.
* **Claiming is atomic.** ``claim`` takes the next due job with
  ``FOR UPDATE SKIP LOCKED``: concurrent workers never pick the same row, and a
  worker that dies after claiming leaves a row whose heartbeat stops, which
  ``requeue_stale`` turns into a retry.
* **Retries are new rows.** A failed job keeps its record (counters, error, timing);
  the retry is a fresh queued row pointing back at it, with exponential backoff and
  jitter. History stays readable and the retry count is visible.

Schedules are 5-field cron expressions evaluated in each repository's own time zone,
once per minute, with ``last_scheduled_at`` preventing a double enqueue when the
scheduler loop runs more than once in the same minute.
"""
from __future__ import annotations

import os
import random
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("incremental", "full", "backfill", "sweep", "export")
TRIGGERS = ("schedule", "manual", "retry", "system")
FINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running")

#: Retry delay for attempt n is between half and all of base * 2**(n-1), capped.
BACKOFF_BASE = 60.0
BACKOFF_CAP = 1800.0


class JobError(Exception):
    """A queue operation that cannot be carried out (bad input, unknown job)."""


# ----------------------------------------------------------------------------- cron

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _expand(spec: str, lo: int, hi: int) -> set[int]:
    """One cron field -> the set of matching values. Supports * , - / and lists."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise JobError(f"bad cron step in {spec!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise JobError(f"bad cron range in {spec!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise JobError(f"bad cron field {spec!r}")
        if start < lo or end > hi or start > end:
            raise JobError(f"cron value out of range in {spec!r} ({lo}-{hi})")
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expr: str, when: datetime) -> bool:
    """Does the 5-field cron expression match this minute?

    Fields: minute hour day-of-month month day-of-week (0 or 7 = Sunday). As in cron,
    when both day fields are restricted either one matching is enough.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise JobError(f"cron needs 5 fields, got {expr!r}")
    minute, hour, dom, month, dow = (
        _expand(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_RANGES)
    )
    if 7 in dow:
        dow.add(0)
    if when.minute not in minute or when.hour not in hour or when.month not in month:
        return False
    weekday = (when.weekday() + 1) % 7  # Python: Monday=0 -> cron: Sunday=0
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    if dom_restricted and dow_restricted:
        return when.day in dom or weekday in dow
    return when.day in dom and weekday in dow


def validate_cron(expr: str) -> None:
    """Raise JobError if the expression is not a usable 5-field cron."""
    cron_matches(expr, datetime(2026, 1, 1, tzinfo=timezone.utc))


def next_run(expr: str, tz: str, now: datetime, horizon_days: int = 62) -> datetime | None:
    """The next minute (UTC) the cron matches in time zone `tz`, after `now`."""
    try:
        zone = ZoneInfo(tz)
        validate_cron(expr)
    except (ZoneInfoNotFoundError, ValueError, JobError):
        return None
    t = now.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    fields = expr.split()
    for _ in range(horizon_days * 24 * 60):
        if cron_matches(expr, t.astimezone(zone)):
            return t
        # skip whole hours that cannot match the minute field's single value
        local_minute = t.astimezone(zone).minute
        if fields[0].isdigit() and local_minute != int(fields[0]):
            t += timedelta(minutes=(int(fields[0]) - local_minute) % 60)
        else:
            t += timedelta(minutes=1)
    return None


def backoff_seconds(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Delay before retry number `attempt` (1-based), with jitter.

    Full-interval jitter keeps retries of several repositories from lining up: the
    wait is a uniform draw from the upper half of the exponential window.
    """
    window = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(attempt - 1, 0)))
    return window / 2 + rng() * (window / 2)


# ------------------------------------------------------------------------ records


@dataclass
class RepoConfig:
    repo: str
    enabled: bool
    collections: list[str]
    schedule_cron: str
    timezone: str
    settings: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    last_scheduled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_job_id: int | None = None


@dataclass
class Job:
    id: int
    repo: str
    kind: str
    trigger: str
    status: str
    attempt: int
    max_attempts: int
    run_after: datetime
    created_at: datetime
    collections: list[str] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    rate: dict[str, Any] = field(default_factory=dict)
    requested_by: str | None = None
    parent_job_id: int | None = None
    worker: str | None = None
    code_version: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    export_id: int | None = None
    priority: int = 100


_JOB_COLUMNS = (
    "id, repo, kind, trigger, status, attempt, max_attempts, run_after, created_at, "
    "collections, counters, checkpoint, rate, requested_by, parent_job_id, worker, "
    "code_version, started_at, heartbeat_at, finished_at, error, export_id, priority"
)


def _job(row: Sequence[Any]) -> Job:
    names = [c.strip() for c in _JOB_COLUMNS.split(",")]
    data = dict(zip(names, row))
    if data.get("requested_by") is not None:
        data["requested_by"] = str(data["requested_by"])
    return Job(**data)


# ---------------------------------------------------------------- sanitizing


_SECRET = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sb_secret_[A-Za-z0-9_]+"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_DSN = re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@")


def sanitize(text: str | None, limit: int = 2000) -> str | None:
    """An error message safe to store and show: no DSN password, no token."""
    if text is None:
        return None
    text = _DSN.sub(r"\1***@", text)
    text = _SECRET.sub("***", text)
    return text[:limit]


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def code_version() -> str:
    """The deployed revision: GITSCOUT_VERSION if the host sets it, else git's short sha."""
    env = os.getenv("GITSCOUT_VERSION") or os.getenv("RENDER_GIT_COMMIT")
    if env:
        return env[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parent,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


# ------------------------------------------------------------------- the store


class JobStore:
    """Queue, history and schedule state on one Postgres connection.

    Use as a context manager; the transaction commits on a clean exit and rolls back
    on an exception, like the push writer. Call ``commit()`` to make a step durable
    mid-way (a worker does so after claiming, and on every heartbeat).
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "gitscout",
        connect_timeout: int = 15,
        clock: Callable[[], datetime] | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", schema):
            raise JobError(f"schema must be a plain identifier, got {schema!r}")
        self._dsn = dsn
        self.schema = schema
        self.connect_timeout = connect_timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._conn: Any = None

    @classmethod
    def wrap(cls, conn: Any, *, schema: str = "gitscout") -> "JobStore":
        """A store on a connection someone else owns (the dashboard's pool). Do not use
        it as a context manager; the owner commits and closes."""
        store = cls("", schema=schema)
        store._conn = conn
        return store

    # --- connection -----------------------------------------------------------
    def __enter__(self) -> "JobStore":
        import psycopg

        try:
            self._conn = psycopg.connect(
                self._dsn, connect_timeout=self.connect_timeout,
                autocommit=False, prepare_threshold=None,
            )
        except Exception as exc:  # noqa: BLE001
            raise JobError(f"could not connect: {sanitize(str(exc))}") from exc
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

    def commit(self) -> None:
        self._conn.commit()

    def _t(self, table: str) -> str:
        return f"{self.schema}.{table}"

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    @staticmethod
    def _jsonb(value: Mapping[str, Any] | None) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(dict(value or {}))

    # --- repositories and schedules ------------------------------------------
    def repositories(self, enabled_only: bool = False) -> list[RepoConfig]:
        where = "where enabled" if enabled_only else ""
        rows = self._rows(
            f"select repo, enabled, collections, schedule_cron, timezone, settings, "
            f"checkpoint, last_scheduled_at, last_success_at, last_job_id "
            f"from {self._t('repositories')} {where} order by repo"
        )
        return [RepoConfig(*r) for r in rows]

    def due(self, now: datetime | None = None) -> list[tuple[RepoConfig, datetime]]:
        """Enabled repositories whose cron matches this minute and were not yet
        enqueued for it. Returns (config, minute in UTC) pairs. A repository with an
        unusable time zone or cron is skipped, never raised on: one bad row must not
        stop the scheduler for the others."""
        now = (now or self._clock()).astimezone(timezone.utc)
        due: list[tuple[RepoConfig, datetime]] = []
        for cfg in self.repositories(enabled_only=True):
            try:
                local = now.astimezone(ZoneInfo(cfg.timezone))
                hit = cron_matches(cfg.schedule_cron, local)
            except (ZoneInfoNotFoundError, JobError, ValueError):
                continue
            if not hit:
                continue
            minute = now.replace(second=0, microsecond=0)
            if cfg.last_scheduled_at is not None and cfg.last_scheduled_at >= minute:
                continue
            due.append((cfg, minute))
        return due

    def mark_scheduled(self, repo: str, minute: datetime) -> None:
        self._rows(
            f"update {self._t('repositories')} set last_scheduled_at = %s where repo = %s",
            [minute, repo],
        )

    def set_repository(
        self,
        repo: str,
        *,
        enabled: bool | None = None,
        schedule_cron: str | None = None,
        tz: str | None = None,
        collections: Sequence[str] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> bool:
        """Change configuration columns. Returns False when the repo is unknown."""
        sets: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            sets.append("enabled = %s"); params.append(enabled)
        if schedule_cron is not None:
            validate_cron(schedule_cron)
            sets.append("schedule_cron = %s"); params.append(schedule_cron)
        if tz is not None:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise JobError(f"unknown time zone {tz!r}") from exc
            sets.append("timezone = %s"); params.append(tz)
        if collections is not None:
            sets.append("collections = %s"); params.append(list(collections))
        if settings is not None:
            sets.append("settings = %s"); params.append(self._jsonb(settings))
        if not sets:
            return True
        params.append(repo)
        with self._conn.cursor() as cur:
            cur.execute(
                f"update {self._t('repositories')} set {', '.join(sets)} where repo = %s", params
            )
            return cur.rowcount == 1

    # --- queue ---------------------------------------------------------------
    def enqueue(
        self,
        repo: str,
        kind: str,
        trigger: str,
        *,
        requested_by: str | None = None,
        run_after: datetime | None = None,
        collections: Sequence[str] | None = None,
        priority: int = 100,
        parent_job_id: int | None = None,
        attempt: int = 0,
        max_attempts: int = 3,
    ) -> int | None:
        """Add a job. Returns its id, or None when the same (repo, kind) already has a
        queued or running job -- the caller decides whether that is news."""
        if kind not in KINDS:
            raise JobError(f"unknown job kind {kind!r}")
        if trigger not in TRIGGERS:
            raise JobError(f"unknown trigger {trigger!r}")
        import psycopg

        with self._conn.cursor() as cur:
            cur.execute("savepoint enqueue")
            try:
                cur.execute(
                    f"insert into {self._t('jobs')} (repo, kind, trigger, requested_by, run_after, "
                    f"collections, priority, parent_job_id, attempt, max_attempts) "
                    f"values (%s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s, %s) returning id",
                    [repo, kind, trigger, requested_by, run_after, list(collections or []),
                     priority, parent_job_id, attempt, max_attempts],
                )
                job_id = cur.fetchone()[0]
            except psycopg.errors.UniqueViolation:
                cur.execute("rollback to savepoint enqueue")
                return None
            cur.execute("release savepoint enqueue")
            return int(job_id)

    def claim(self, worker: str | None = None, version: str | None = None) -> Job | None:
        """Take the next due job for this worker, or None. Safe under concurrency."""
        rows = self._rows(
            f"update {self._t('jobs')} j set status = 'running', started_at = now(), "
            f"heartbeat_at = now(), attempt = attempt + 1, worker = %s, code_version = %s "
            f"where j.id = (select id from {self._t('jobs')} where status = 'queued' "
            f"and run_after <= now() order by priority, run_after, id limit 1 "
            f"for update skip locked) returning {_JOB_COLUMNS}",
            [worker or worker_name(), version or code_version()],
        )
        return _job(rows[0]) if rows else None

    def heartbeat(
        self,
        job_id: int,
        *,
        counters: Mapping[str, Any] | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        rate: Mapping[str, Any] | None = None,
    ) -> str:
        """Record liveness and progress; returns the job's current status so a worker
        notices a cancellation request."""
        sets = ["heartbeat_at = now()"]
        params: list[Any] = []
        for col, val in (("counters", counters), ("checkpoint", checkpoint), ("rate", rate)):
            if val is not None:
                sets.append(f"{col} = %s"); params.append(self._jsonb(val))
        params.append(job_id)
        rows = self._rows(
            f"update {self._t('jobs')} set {', '.join(sets)} where id = %s returning status", params
        )
        if not rows:
            raise JobError(f"job {job_id} does not exist")
        return rows[0][0]

    def finish(
        self,
        job_id: int,
        status: str,
        *,
        counters: Mapping[str, Any] | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        rate: Mapping[str, Any] | None = None,
        error: str | None = None,
        export_id: int | None = None,
    ) -> int | None:
        """Close a job. A failure with attempts left enqueues the retry and returns its
        id; a success records the repository's last success and checkpoint."""
        if status not in FINAL:
            raise JobError(f"finish needs a final status, got {status!r}")
        rows = self._rows(
            f"update {self._t('jobs')} set status = %s, finished_at = now(), "
            f"counters = coalesce(%s, counters), checkpoint = coalesce(%s, checkpoint), "
            f"rate = coalesce(%s, rate), error = %s, export_id = coalesce(%s, export_id) "
            f"where id = %s and status = 'running' returning {_JOB_COLUMNS}",
            [status,
             self._jsonb(counters) if counters is not None else None,
             self._jsonb(checkpoint) if checkpoint is not None else None,
             self._jsonb(rate) if rate is not None else None,
             sanitize(error), export_id, job_id],
        )
        if not rows:
            raise JobError(f"job {job_id} is not running")
        job = _job(rows[0])
        if status == "succeeded":
            self._rows(
                f"update {self._t('repositories')} set last_success_at = now(), last_job_id = %s, "
                f"checkpoint = coalesce(%s, checkpoint) where repo = %s",
                [job.id, self._jsonb(checkpoint) if checkpoint is not None else None, job.repo],
            )
            return None
        if status == "failed" and job.attempt < job.max_attempts:
            delay = backoff_seconds(job.attempt, self._rng)
            return self.enqueue(
                job.repo, job.kind, "retry",
                run_after=self._clock() + timedelta(seconds=delay),
                collections=job.collections, priority=job.priority, parent_job_id=job.id,
                attempt=job.attempt, max_attempts=job.max_attempts,
                requested_by=job.requested_by,
            )
        return None

    def requeue_stale(self, older_than: timedelta) -> list[tuple[int, int | None]]:
        """Running jobs whose heartbeat stopped: fail them (the worker is gone) and
        enqueue their retries. Returns (job id, retry id or None) pairs."""
        minutes = max(1, int(older_than.total_seconds() // 60))
        stale = self._rows(
            f"select id from {self._t('jobs')} where status = 'running' "
            f"and heartbeat_at < now() - %s::interval order by id",
            [f"{minutes} minutes"],
        )
        out = []
        for (job_id,) in stale:
            retry = self.finish(
                job_id, "failed",
                error=f"stalled: no heartbeat for {minutes} minutes; the worker is presumed dead",
            )
            out.append((job_id, retry))
        return out

    def cancel(self, job_id: int) -> bool:
        """Cancel a queued job outright, or ask a running one to stop (its worker sees
        the status on the next heartbeat). False when it is already finished."""
        rows = self._rows(
            f"update {self._t('jobs')} set status = 'cancelled', "
            f"finished_at = case when status = 'queued' then now() else finished_at end "
            f"where id = %s and status in ('queued', 'running') returning id",
            [job_id],
        )
        return bool(rows)

    # --- reading -------------------------------------------------------------
    def get(self, job_id: int) -> Job | None:
        rows = self._rows(f"select {_JOB_COLUMNS} from {self._t('jobs')} where id = %s", [job_id])
        return _job(rows[0]) if rows else None

    def list(self, *, repo: str | None = None, status: str | None = None, limit: int = 50) -> list[Job]:
        where, params = [], []
        if repo:
            where.append("repo = %s"); params.append(repo)
        if status:
            where.append("status = %s"); params.append(status)
        clause = ("where " + " and ".join(where)) if where else ""
        params.append(limit)
        rows = self._rows(
            f"select {_JOB_COLUMNS} from {self._t('jobs')} {clause} order by id desc limit %s", params
        )
        return [_job(r) for r in rows]

    def settings(self) -> dict[str, Any]:
        return {k: v for k, v in self._rows(f"select key, value from {self._t('app_settings')}")}

    # --- liveness and health (migration 0013) ----------------------------------
    def seen(self, name: str, version: str, *, jobs_run: int = 0, current_job_id: int | None = None,
             stopped: bool = False) -> bool:
        """Record that this worker is alive. Savepointed: before migration 0013 the
        table is missing and this quietly returns False instead of aborting the tick."""
        try:
            self._rows("savepoint seen")
            self._rows(
                f"insert into {self._t('workers')} (name, version, jobs_run, current_job_id, stopped_at) "
                f"values (%s, %s, %s, %s, case when %s then now() end) "
                f"on conflict (name) do update set version = excluded.version, seen_at = now(), "
                f"jobs_run = excluded.jobs_run, current_job_id = excluded.current_job_id, "
                f"stopped_at = excluded.stopped_at",
                [name, version, jobs_run, current_job_id, stopped],
            )
            self._rows("release savepoint seen")
            return True
        except Exception:  # noqa: BLE001
            self._rows("rollback to savepoint seen")
            return False

    def _dicts(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d.name for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def health(self) -> list[dict[str, Any]]:
        return self._dicts(f"select * from {self._t('repo_health')} order by repo")

    def workers(self) -> list[dict[str, Any]]:
        return self._dicts(f"select *, extract(epoch from now() - seen_at)::integer as silent_seconds "
                           f"from {self._t('workers')} order by seen_at desc")

    def alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._dicts(f"select id, at, key, severity, repo, job_id, subject, channel, sent_at, error "
                           f"from {self._t('alerts')} order by id desc limit %s", [limit])

    # --- audit ---------------------------------------------------------------
    def audit(
        self,
        action: str,
        *,
        actor_id: str | None = None,
        actor_email: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        repo: str | None = None,
        affected: int | None = None,
        details: Mapping[str, Any] | None = None,
        result: str = "ok",
    ) -> None:
        self._rows(
            f"insert into {self._t('audit_log')} (actor_id, actor_email, action, target_kind, "
            f"target_id, repo, affected, details, result) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [actor_id, actor_email, action, target_kind, target_id, repo, affected,
             self._jsonb(details), result],
        )
