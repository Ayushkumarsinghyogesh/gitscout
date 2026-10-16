"""Phase 7 exports: CSV cells, date ranges by collection and by activity, the nightly
archive into Storage with retention, the worker's export job, and the CLI download."""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
from datetime import date, datetime, timezone

import httpx
import psycopg
import pytest
from typer.testing import CliRunner

from gitscout import cli
from gitscout.config import Settings
from gitscout.exports import (EXCEL_CELL, ExportError, ExportRequest, Storage, cell, iter_csv,
                              nightly_export, write_csv)
from gitscout.jobs import JobStore
from gitscout.worker import Worker

UTC = timezone.utc


# ------------------------------------------------------------------ cells


def test_cells_are_spreadsheet_safe():
    assert cell("=HYPERLINK(\"x\")", excel_safe=True) == "'=HYPERLINK(\"x\")"
    assert cell("-rm -rf", excel_safe=True) == "'-rm -rf"
    assert cell(-5, excel_safe=True) == "-5"                         # numbers are not formulas
    assert cell(["forks", "issues"], excel_safe=True) == "forks;issues"
    assert json.loads(cell([{"n": 1, "body": "héllo"}], excel_safe=True)) == [{"n": 1, "body": "héllo"}]
    assert cell(None, excel_safe=True) == "" and cell(True, excel_safe=True) == "true"
    long = "x" * 50_000
    short = cell(long, excel_safe=True)
    assert len(short) == EXCEL_CELL and "50,000 characters" in short
    assert cell(long, excel_safe=False) == long


def test_day_ranges_are_whole_days_in_the_time_zone():
    start, end = ExportRequest(date_from=date(2026, 10, 1), date_to=date(2026, 10, 15)).bounds()
    assert start == datetime(2026, 9, 30, 18, 30, tzinfo=UTC)      # 1 Oct 00:00 IST
    assert end == datetime(2026, 10, 15, 18, 30, tzinfo=UTC)       # 16 Oct 00:00 IST, exclusive
    with pytest.raises(ExportError, match="after"):
        ExportRequest(date_from=date(2026, 10, 2), date_to=date(2026, 10, 1)).bounds()
    with pytest.raises(ExportError, match="basis"):
        ExportRequest(basis="scraped").bounds()
    assert ExportRequest(repo="o/r").filename(datetime(2026, 10, 16, 9, 0, tzinfo=UTC)) == \
        "gitscout-o-r-collected-all-time-20261016-0900.csv"


# ---------------------------------------------------------------- dataset


@pytest.fixture
def dataset(pg_dsn):
    """Three people in o/r: active in 2024, 2025 and 2026, collected 1, 10 and 20 Oct."""
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("select gitscout.ensure_repo_schema('o/r')")
        c.execute('delete from "Defendermate-Github1-R".interactions; delete from gitscout.dataset_first_collected')
        c.execute("delete from gitscout.people where login in ('ann', 'bob', 'cy')")
        c.execute("insert into gitscout.people (login, name, best_email, bio) values "
                  "('ann', 'Ann A', 'ann@a.dev', '=cmd|calc'), ('bob', 'Bob B', null, null), ('cy', 'Cy', null, null)")
        c.execute('insert into "Defendermate-Github1-R".interactions (repo, login, kind, occurred_at, extra) values '
                  "('o/r', 'ann', 'forks', '2024-05-01T00:00:00Z', 'ann/r'),"
                  "('o/r', 'bob', 'stars', '2025-05-01T00:00:00Z', null),"
                  "('o/r', 'cy',  'stars', '2026-05-01T00:00:00Z', null)")
        c.execute("select gitscout.refresh_outreach()")
        c.execute("update gitscout.dataset_first_collected set collected_at = case login "
                  "when 'ann' then timestamptz '2026-10-01T06:00:00Z' when 'bob' then timestamptz '2026-10-10T06:00:00Z' "
                  "else timestamptz '2026-10-20T06:00:00Z' end where repo = 'o/r'")
    yield pg_dsn
    with psycopg.connect(pg_dsn, autocommit=True) as c:          # leave the session database as found
        c.execute('delete from "Defendermate-Github1-R".interactions; delete from gitscout.dataset_first_collected; '
                  "delete from gitscout.people where login in ('ann', 'bob', 'cy'); delete from gitscout.exports; "
                  "delete from gitscout.jobs")
        c.execute("select gitscout.refresh_outreach()")


def rows_of(dsn, req: ExportRequest) -> list[dict]:
    with psycopg.connect(dsn) as conn:
        text = "".join(iter_csv(conn, req))
    assert text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(text[1:])))


def test_refresh_records_when_each_person_was_first_collected(dataset):
    with psycopg.connect(dataset, autocommit=True) as c:
        before = c.execute("select login, collected_at from gitscout.dataset_first_collected order by 1").fetchall()
        c.execute("select gitscout.refresh_outreach()")                 # a second refresh keeps the dates
        after = c.execute("select login, collected_at from gitscout.dataset_first_collected order by 1").fetchall()
    assert [r[0] for r in before] == ["ann", "bob", "cy"] and before == after


def test_export_everything_and_by_collection_date(dataset):
    every = rows_of(dataset, ExportRequest(repo="o/r"))
    assert [r["username"] for r in every] == ["ann", "bob", "cy"]
    assert every[0]["bio"] == "'=cmd|calc" and every[0]["email"] == "ann@a.dev"
    assert every[0]["first_collected_at"].startswith("2026-10-01")
    oct_5_to_15 = rows_of(dataset, ExportRequest(repo="o/r", date_from=date(2026, 10, 5), date_to=date(2026, 10, 15)))
    assert [r["username"] for r in oct_5_to_15] == ["bob"]
    until_10th = rows_of(dataset, ExportRequest(date_to=date(2026, 10, 10)))
    assert [r["username"] for r in until_10th if r["repository_name"] == "r"] == ["ann", "bob"]


def test_export_by_github_activity(dataset):
    active_2025 = rows_of(dataset, ExportRequest(repo="o/r", basis="activity",
                                                 date_from=date(2025, 1, 1), date_to=date(2025, 12, 31)))
    assert [r["username"] for r in active_2025] == ["bob"]
    assert rows_of(dataset, ExportRequest(repo="o/r", date_from=date(2030, 1, 1))) == []


def test_write_csv_plain_and_gzipped(dataset, tmp_path):
    rows, size = write_csv(dataset, ExportRequest(repo="o/r"), str(tmp_path / "a.csv"))
    assert rows == 3 and size > 0
    rows, _ = write_csv(dataset, ExportRequest(repo="o/r"), str(tmp_path / "a.csv.gz"), gzipped=True)
    with gzip.open(tmp_path / "a.csv.gz", "rt", encoding="utf-8") as fh:
        assert rows == 3 and len(list(csv.reader(fh))) == 4


def test_cli_downloads_a_range_to_a_local_file(dataset, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_DB_URL", dataset)
    monkeypatch.setenv("GITSCOUT_DB", str(tmp_path / "x.db"))
    out = tmp_path / "range.csv"
    result = CliRunner().invoke(cli.app, ["dataset", "export", "--repo", "o/r", "--from", "2026-10-01",
                                          "--to", "2026-10-10", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "wrote 2 rows" in result.output and out.read_text(encoding="utf-8-sig").count("\n") == 3
    bad = CliRunner().invoke(cli.app, ["dataset", "export", "--from", "10/01/2026"])
    assert bad.exit_code == 2 and "YYYY-MM-DD" in bad.output


# ----------------------------------------------------------------- archive


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "sb_secret_test" and request.headers["authorization"] == "Bearer sb_secret_test"
        path = request.url.path.split("/storage/v1/object/", 1)[1]
        if request.method == "POST":
            self.objects[path.split("/", 1)[1]] = request.content
            return httpx.Response(200, json={"Key": path})
        if request.method == "DELETE":
            self.deleted += json.loads(request.content)["prefixes"]
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def test_nightly_export_uploads_records_and_expires_old_files(dataset):
    fake = FakeStorage()
    now = datetime(2026, 10, 17, 20, 30, tzinfo=UTC)
    with psycopg.connect(dataset, autocommit=True) as c:
        c.execute("delete from gitscout.exports")
        c.execute("insert into gitscout.exports (repo, format, storage_path, expires_at) values "
                  "('o/r', 'csv', 'o/r/2026/01/old.csv.gz', '2026-04-01T00:00:00Z')")
    with JobStore(dataset) as store:
        out = nightly_export(dataset, store, Storage("https://x.supabase.co", "sb_secret_test", transport=fake.transport),
                             "o/r", job_id=None, retention_days=90, clock=lambda: now)
    (key, body), = fake.objects.items()
    assert key.startswith("o/r/2026/10/gitscout-o-r-collected-all-time-") and key.endswith(".csv.gz")
    assert gzip.decompress(body).decode("utf-8-sig").count("\n") == 4
    assert out["export"]["rows"] == 3 and out["export"]["expired_removed"] == 1
    assert fake.deleted == ["o/r/2026/01/old.csv.gz"]
    with psycopg.connect(dataset) as c:
        rows = c.execute("select storage_path, deleted_at is not null, expires_at::date from gitscout.exports order by id").fetchall()
    assert rows[0][1] is True and rows[1][1] is False and rows[1][2] == date(2027, 1, 15)


def test_the_worker_runs_the_export_at_two_in_the_morning(dataset, gql, tmp_path):
    fake = FakeStorage()
    with psycopg.connect(dataset, autocommit=True) as c:
        c.execute("delete from gitscout.jobs; delete from gitscout.exports")
        c.execute("update gitscout.repositories set enabled = (repo = 'o/r'), timezone = 'UTC', "
                  "schedule_cron = '0 0 * * *', settings = '{}' ")
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "w.db"), supabase_db_url=dataset,
                        supabase_url="https://x.supabase.co", supabase_key="sb_secret_test")
    worker = Worker(settings, name="t:1", version="v", transport=gql.transport,
                    clock=lambda: datetime(2026, 10, 17, 2, 0, 5, tzinfo=UTC))
    worker.storage_transport = fake.transport
    assert asyncio.run(worker.tick()) is True
    with psycopg.connect(dataset) as c:
        (kind, status, counters), = c.execute("select kind, status, counters from gitscout.jobs").fetchall()
        assert (kind, status, counters["export"]["rows"]) == ("export", "succeeded", 3)
        assert c.execute("select count(*) from gitscout.exports where deleted_at is null").fetchone()[0] == 1
        assert c.execute("select settings->>'last_export_minute' from gitscout.repositories where repo = 'o/r'"
                         ).fetchone()[0] == "2026-10-17T02:00:00+00:00"
    assert asyncio.run(worker.tick()) is False                          # same minute: not again
    assert len(fake.objects) == 1
