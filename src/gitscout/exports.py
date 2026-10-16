"""Exports of the outreach dataset: a CSV of every record, or of a date range.

One generator, ``iter_csv``, produces the file; everything else is a way to deliver
it. The dashboard streams it to the browser (a download), ``gitscout dataset export``
writes it to a local file, and the nightly ``export`` job gzips it into the private
Storage bucket and records it in ``gitscout.exports``.

Date ranges are whole days in the given time zone (default Asia/Kolkata), both ends
inclusive. Which date is filtered on is the *basis*:

* ``collected`` (default) - when GitScout first put the person into the dataset
  (``gitscout.dataset_first_collected``, migration 0014): "what did we find between
  these dates"
* ``activity`` - the person's GitHub activity in the repository overlaps the range
  (``first_seen_at`` .. ``last_seen_at``): "who was active between these dates"

Two things make the file safe to open in a spreadsheet:

* Cells that start with ``= + - @`` (formula triggers) get a leading apostrophe. Text
  comes from GitHub, so anyone could otherwise plant a formula in it.
* ``excel_safe`` caps a cell at 32,000 characters, under Excel's hard limit of 32,767.
  A shortened cell ends with a marker giving its full length. Without it the file is
  complete, but rows with very long thread histories break in Excel.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .jobs import JobError, JobStore, sanitize

log = logging.getLogger(__name__)

DATASET = '"Defendermate2".final_outreach_users'
BUCKET = "gitscout-exports"
BASES = ("collected", "activity")
EXCEL_CELL = 32_000
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


class ExportError(JobError):
    """A request for an export that cannot be served (bad dates, unknown repository)."""


@dataclass
class ExportRequest:
    repo: str | None = None              # owner/name, or None for every repository
    date_from: date | None = None
    date_to: date | None = None
    basis: str = "collected"
    tz: str = "Asia/Kolkata"
    excel_safe: bool = True
    #: Restrict to these repositories (the dashboard passes what the viewer may see);
    #: None = no restriction. An empty list matches nothing.
    repos: Sequence[str] | None = None

    def bounds(self) -> tuple[datetime | None, datetime | None]:
        """[start, end) in UTC for the inclusive day range."""
        if self.basis not in BASES:
            raise ExportError(f"basis must be one of {', '.join(BASES)}")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ExportError("'from' is after 'to'")
        try:
            zone = ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ExportError(f"unknown time zone {self.tz!r}") from exc
        start = datetime.combine(self.date_from, time.min, zone).astimezone(timezone.utc) if self.date_from else None
        end = (datetime.combine(self.date_to + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
               if self.date_to else None)
        return start, end

    def filters(self) -> dict[str, Any]:
        return {"repo": self.repo, "from": self.date_from.isoformat() if self.date_from else None,
                "to": self.date_to.isoformat() if self.date_to else None, "basis": self.basis,
                "tz": self.tz, "excel_safe": self.excel_safe}

    def filename(self, stamp: datetime | None = None) -> str:
        slug = (self.repo or "all-repositories").replace("/", "-")
        span = (f"{self.date_from or 'start'}_to_{self.date_to or 'today'}"
                if (self.date_from or self.date_to) else "all-time")
        when = (stamp or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
        return f"gitscout-{slug}-{self.basis}-{span}-{when}.csv"


def parse_day(value: str | None) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ExportError(f"dates are YYYY-MM-DD, got {value!r}") from exc


def query(req: ExportRequest) -> tuple[str, list[Any]]:
    """The SELECT for a request: every dataset column plus first_collected_at."""
    start, end = req.bounds()
    where: list[str] = []
    params: list[Any] = []
    if req.repo:
        if "/" not in req.repo:
            raise ExportError("repository is owner/name")
        owner, name = req.repo.split("/", 1)
        where.append("d.repository_owner = %s and d.repository_name = %s")
        params += [owner, name]
    if req.repos is not None:
        where.append("d.repository_owner || '/' || d.repository_name = any(%s)")
        params.append(list(req.repos))
    if req.basis == "collected":
        # a person not yet in the first-collected table arrived after the last refresh
        col = "coalesce(fc.collected_at, now())"
        if start:
            where.append(f"{col} >= %s"); params.append(start)
        if end:
            where.append(f"{col} < %s"); params.append(end)
    else:
        if start:
            where.append("d.last_seen_at >= %s"); params.append(start)
        if end:
            where.append("d.first_seen_at < %s"); params.append(end)
    clause = ("where " + " and ".join(where)) if where else ""
    sql = (f"select d.*, fc.collected_at as first_collected_at from {DATASET} d "
           f"left join gitscout.dataset_first_collected fc "
           f"on fc.repo = d.repository_owner || '/' || d.repository_name and fc.login = d.username "
           f"{clause} order by d.repository_owner, d.repository_name, d.username")
    return sql, params


def cell(value: Any, *, excel_safe: bool) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, datetime):
        text = value.isoformat()
    elif isinstance(value, (list, tuple)) and all(isinstance(v, (str, int)) or v is None for v in value):
        text = ";".join("" if v is None else str(v) for v in value)
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    if isinstance(value, str) and text.startswith(_FORMULA):
        text = "'" + text
    if excel_safe and len(text) > EXCEL_CELL:
        marker = f" ...[shortened for Excel: {len(text):,} characters; export without Excel-safe for all of it]"
        text = text[: EXCEL_CELL - len(marker)] + marker
    return text


def iter_csv(conn: Any, req: ExportRequest, *, batch: int = 500, counter: list[int] | None = None) -> Iterator[str]:
    """Yield the CSV in chunks: a UTF-8 BOM (so Excel reads accents and emoji), the
    header, then rows. Reads with a server-side cursor; memory stays flat however
    large the dataset is. `counter`, if given, receives the row count."""
    sql, params = query(req)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    rows = 0
    with conn.cursor(name="gitscout_export") as cur:
        cur.itersize = batch
        cur.execute(sql, params)
        header = None
        yield "﻿"
        for row in cur:
            if header is None:
                header = [d.name for d in cur.description]
                writer.writerow(header)
            writer.writerow([cell(v, excel_safe=req.excel_safe) for v in row])
            rows += 1
            if out.tell() > 256_000:
                yield out.getvalue(); out.seek(0); out.truncate()
        if header is None:  # no rows: still a valid file with a header
            writer.writerow([d.name for d in cur.description])
    yield out.getvalue()
    if counter is not None:
        counter.append(rows)


def write_csv(dsn: str, req: ExportRequest, path: str, *, gzipped: bool = False) -> tuple[int, int]:
    """Write the export to a local file. Returns (rows, bytes on disk)."""
    import psycopg

    counter: list[int] = []
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8", newline="")) if gzipped else \
             (lambda p: open(p, "w", encoding="utf-8", newline=""))
    with psycopg.connect(dsn, prepare_threshold=None) as conn, opener(path) as fh:
        for chunk in iter_csv(conn, req, counter=counter):
            fh.write(chunk)
    return counter[0], os.path.getsize(path)


# ------------------------------------------------------------- Storage


class Storage:
    """The few Supabase Storage calls exports need, with the service key."""

    def __init__(self, url: str, key: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.base = url.rstrip("/") + "/storage/v1"
        # New-style secret keys go in `apikey`; Authorization must then carry the same value.
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=300, transport=self.transport, headers=self.headers)

    def upload(self, path: str, file_path: str, content_type: str = "application/gzip") -> None:
        with self._client() as http, open(file_path, "rb") as fh:
            r = http.post(f"{self.base}/object/{BUCKET}/{path}", content=fh.read(),
                          headers={"Content-Type": content_type, "x-upsert": "true"})
        if r.status_code >= 300:
            raise ExportError(f"Storage upload refused ({r.status_code}): {sanitize(r.text[:300])}")

    def delete(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        with self._client() as http:
            r = http.request("DELETE", f"{self.base}/object/{BUCKET}", json={"prefixes": list(paths)})
        if r.status_code >= 300:
            raise ExportError(f"Storage delete refused ({r.status_code}): {sanitize(r.text[:300])}")

    def signed_url(self, path: str, seconds: int = 3600) -> str:
        with self._client() as http:
            r = http.post(f"{self.base}/object/sign/{BUCKET}/{path}", json={"expiresIn": seconds})
        if r.status_code >= 300:
            raise ExportError(f"Storage sign refused ({r.status_code}): {sanitize(r.text[:300])}")
        return self.base + r.json()["signedURL"]


def nightly_export(dsn: str, store: JobStore, storage: Storage, repo: str, *, job_id: int | None,
                   retention_days: int, clock=None) -> dict[str, Any]:
    """All-time export of one repository, gzipped into Storage and recorded. Then the
    retention sweep. Returns the counters the job records."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    req = ExportRequest(repo=repo, excel_safe=False)          # the archive keeps every character
    name = req.filename(now) + ".gz"
    key = f"{repo}/{now:%Y/%m}/{name}"
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, name)
        rows, size = write_csv(dsn, req, local, gzipped=True)
        storage.upload(key, local)
    store._rows(
        f"insert into {store._t('exports')} (repo, job_id, format, storage_path, row_count, byte_size, "
        f"filters, generated_at, expires_at) values (%s, %s, 'csv', %s, %s, %s, %s, %s, %s)",
        [repo, job_id, key, rows, size, store._jsonb(req.filters()), now,
         now + timedelta(days=retention_days)],
    )
    removed = purge_expired(store, storage, now)
    return {"export": {"rows": rows, "bytes": size, "path": key, "expired_removed": removed}}


def purge_expired(store: JobStore, storage: Storage, now: datetime) -> int:
    """Delete export files past expires_at from Storage; mark their rows. The rows stay."""
    rows = store._rows(
        f"select id, storage_path from {store._t('exports')} "
        f"where expires_at < %s and deleted_at is null order by id limit 200", [now])
    if not rows:
        return 0
    storage.delete([p for _, p in rows])
    store._rows(f"update {store._t('exports')} set deleted_at = %s where id = any(%s)",
                [now, [i for i, _ in rows]])
    return len(rows)


def recent(store: JobStore, repo: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    where, params = ["deleted_at is null"], []
    if repo:
        where.append("repo = %s"); params.append(repo)
    params.append(limit)
    return store._dicts(
        f"select id, repo, job_id, storage_path, row_count, byte_size, filters, generated_at, expires_at "
        f"from {store._t('exports')} where {' and '.join(where)} order by id desc limit %s", params)
