"""The deletion sweep: marks what GitHub no longer lists, restores what came back,
marks nothing when the listing is incomplete, and the worker pushes the marks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import psycopg
import pytest
from gqlhelpers import DEFAULT_RATE_LIMIT

from gitscout.config import Settings
from gitscout.jobs import JobStore
from gitscout.models import ThreadRecord
from gitscout.storage import Store
from gitscout.sweep import live_numbers, sweep_repo
from gitscout.worker import Worker

UTC = timezone.utc


def numbers_page(connection: str, numbers, *, has_next=False, cursor=None, total=None):
    return {"repository": {connection: {
        "totalCount": total if total is not None else len(numbers),
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [{"number": n} for n in numbers],
    }}}


def seed_threads(store: Store, repo: str, kind: str, numbers):
    store.upsert_threads(
        ThreadRecord(repo, kind, n, title=f"#{n}", author="alice",
                     created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")
        for n in numbers
    )


def test_sweep_marks_missing_and_restores_relisted(gql, make_gql_client, store):
    seed_threads(store, "o/r", "issues", [1, 2, 3, 4])
    store.mark_threads_deleted("o/r", "issues", [4])                      # marked last week, back now
    gql.add_pages("IssueNumbers", [
        numbers_page("issues", [1, 2], has_next=True, cursor="c1", total=3),
        numbers_page("issues", [4], total=3),
    ])

    async def go():
        async with make_gql_client(gql) as client:
            return await sweep_repo(client, store, "o/r", ["issues"])

    report = asyncio.run(go())
    assert report["issues"]["complete"] is True and report["issues"]["live"] == 3
    assert report["issues"]["marked"] == 1 and report["issues"]["restored"] == 1
    assert store.thread_numbers("o/r", "issues") == {1, 2, 4}
    assert store.deleted_thread_numbers("o/r", "issues") == {3}
    row = store._conn.execute("select source_deleted_at, fetched_at from threads where number = 3").fetchone()
    assert row[0] and row[1] == row[0]                                   # the push watermark moves with the mark


def test_an_incomplete_listing_marks_nothing(gql, make_gql_client, store):
    seed_threads(store, "o/r", "issues", [1, 2, 3])
    gql.add_status("IssueNumbers", 401)                                  # fatal mid-listing

    async def go():
        async with make_gql_client(gql) as client:
            return await sweep_repo(client, store, "o/r", ["issues", "prs"])

    report = asyncio.run(go())
    assert report["issues"]["complete"] is False and report["issues"]["marked"] == 0
    assert "error" in report["issues"]
    assert store.thread_numbers("o/r", "issues") == {1, 2, 3}
    # prs: the repo has none; an unscripted op reads as NOT_FOUND = nothing listed
    assert report["prs"]["marked"] == 0


def test_an_empty_kind_is_complete(gql, make_gql_client, store):
    gql.add("DiscussionNumbers", numbers_page("discussions", [], total=0))

    async def go():
        async with make_gql_client(gql) as client:
            return await live_numbers(client, "o/r", "discussions")

    assert asyncio.run(go()) == (set(), True, 0)


@pytest.fixture
def pg_clean(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("delete from gitscout.jobs; delete from gitscout.audit_log")
        c.execute("update gitscout.repositories set enabled = (repo = 'o/r'), collections = '{issues}', "
                  "schedule_cron = '0 0 * * *', timezone = 'UTC', settings = '{}', checkpoint = '{}', "
                  "last_scheduled_at = null")
        c.execute("select gitscout.ensure_repo_schema('o/r')")
        c.execute('delete from "Defendermate-Github1-R".threads')
    return pg_dsn


def test_a_sweep_job_pushes_the_marks_to_postgres(gql, pg_clean, tmp_path):
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "s.db"), supabase_db_url=pg_clean,
                        worker_heartbeat_seconds=1)
    with Store(settings.db_path) as local:
        seed_threads(local, "o/r", "issues", [10, 11])
        local.upsert_repository({"repo": "o/r", "repository_id": 1, "node_id": "R_1", "owner": "o", "name": "r",
                                 "url": "https://github.com/o/r", "description": None, "homepage": None,
                                 "stars": 1, "forks": 0, "language": None, "topics": "[]", "is_archived": 0})
    gql.add("IssueNumbers", numbers_page("issues", [10]))
    worker = Worker(settings, name="t:1", version="v", transport=gql.transport,
                    clock=lambda: datetime(2026, 10, 18, 1, 0, 10, tzinfo=UTC))   # Sunday 01:00 UTC: sweep is due
    assert asyncio.run(worker.tick()) is True
    with psycopg.connect(pg_clean, autocommit=True) as c:
        jobs = c.execute("select kind, trigger, status, counters from gitscout.jobs order by id").fetchall()
        assert [j[:3] for j in jobs] == [("sweep", "schedule", "succeeded")]   # 01:00: only the sweep is due
        assert jobs[0][3]["sweep"]["issues"]["marked"] == 1 and jobs[0][3]["push"]["threads"] == 2
        marks = c.execute('select number, source_deleted_at is not null from "Defendermate-Github1-R".threads order by 1').fetchall()
        assert marks == [(10, False), (11, True)]
        settings_row = c.execute("select settings->>'last_sweep_minute' from gitscout.repositories where repo = 'o/r'").fetchone()
        assert settings_row[0] == "2026-10-18T01:00:00+00:00"
    assert asyncio.run(worker.tick()) is False                           # same minute: sweep not re-enqueued
