"""The worker end to end: a schedule fires, a job is claimed, GitHub (faked) is
crawled, the result is pushed into a real Postgres, and the job row tells the story.
Failures turn into retries; cancellation and shutdown are recorded."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from gqlhelpers import commit, commit_probe, forks_body, gql_user

from gitscout.config import Settings
from gitscout.jobs import JobError, JobStore
from gitscout.storage import Store
from gitscout.worker import Outcome, Worker, counters_from, engine_kinds, restore_checkpoint

UTC = timezone.utc
SECRET = "ghp_SECRETsecretSECRETsecret0123456789"


def repo_info(owner="o", name="r", database_id=1):
    return {"repository": {
        "id": f"R_{database_id}", "databaseId": database_id, "nameWithOwner": f"{owner}/{name}",
        "name": name, "owner": {"login": owner}, "url": f"https://github.com/{owner}/{name}",
        "description": None, "homepageUrl": None, "stargazerCount": 10, "forkCount": 2,
        "isArchived": False, "primaryLanguage": {"name": "Go"}, "repositoryTopics": {"nodes": []},
    }}


@pytest.fixture
def env(pg_dsn, gql, tmp_path):
    """A worker against the fake GitHub and the session Postgres; o/r collects forks only."""
    settings = Settings(
        tokens=(SECRET,), db_path=str(tmp_path / "w.db"), supabase_db_url=pg_dsn,
        worker_heartbeat_seconds=1, worker_poll_seconds=10,
    )
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("delete from gitscout.jobs; delete from gitscout.audit_log")
        c.execute("update gitscout.repositories set enabled = (repo = 'o/r'), collections = '{forks}', "
                  "schedule_cron = '0 0 * * *', timezone = 'UTC', settings = '{}', checkpoint = '{}', "
                  "last_scheduled_at = null, last_success_at = null, last_job_id = null")
    gql.add("Forks", forks_body([(gql_user("carol", name="Carol C"), "2024-03-01T00:00:00Z")]))
    gql.add("CommitEmails", {"u0": commit_probe("carol", repos=[[commit("carol@c.dev", linked="carol")]])})
    gql.add("Social", {"u0": {"login": "carol", "repositories": {"totalCount": 4},
                              "socialAccounts": {"nodes": [{"provider": "LINKEDIN", "url": "https://linkedin.com/in/carol-c"}]}}})
    gql.add("RepoInfo", repo_info())
    worker = Worker(settings, name="test:1", version="deadbeef", transport=gql.transport,
                    clock=lambda: datetime(2026, 10, 17, 0, 0, 20, tzinfo=UTC))
    return settings, worker, gql


def q(dsn, sql, params=()):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def test_a_due_schedule_becomes_a_finished_job_with_data_in_postgres(env, pg_dsn):
    settings, worker, gql = env
    assert asyncio.run(worker.tick()) is True                      # 00:00 UTC matches '0 0 * * *'
    (job,) = q(pg_dsn, "select status, trigger, kind, attempt, worker, code_version, counters, rate, checkpoint, error from gitscout.jobs")
    status, trigger, kind, attempt, who, version, counters, rate, checkpoint, error = job
    assert (status, trigger, kind, attempt, who, version, error) == ("succeeded", "schedule", "incremental", 1, "test:1", "deadbeef", None)
    assert counters["interactions"]["discovered"] == 1 and counters["people"]["new"] == 1
    assert counters["emails"]["new"] == 1 and counters["social"] == {"looked_up": 1, "linkedin": 1}
    assert counters["push"]["refreshed"] is True and counters["push"]["people"] == 1
    assert rate["points_used"] > 0 and "gql:o/r:forks" in checkpoint
    # the repository row and the mirror both moved
    (repo,) = q(pg_dsn, "select last_success_at is not null, last_job_id is not null, checkpoint ? 'gql:o/r:forks', last_scheduled_at from gitscout.repositories where repo = 'o/r'")
    assert repo[:3] == (True, True, True) and repo[3] == datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
    assert q(pg_dsn, "select login, best_email, linkedin_url from gitscout.people")[0] == ("carol", "carol@c.dev", "https://www.linkedin.com/in/carol-c")
    assert q(pg_dsn, 'select count(*) from "Defendermate-Github1-R".interactions')[0][0] == 1
    assert [a[0] for a in q(pg_dsn, "select action from gitscout.audit_log order by id")] == ["job.succeeded"]
    assert asyncio.run(worker.tick()) is False                     # same minute: nothing new to do
    assert q(pg_dsn, "select count(*) from gitscout.jobs")[0][0] == 1


def test_a_second_run_restores_cursors_from_postgres_on_an_empty_disk(env, pg_dsn, tmp_path):
    settings, worker, gql = env
    assert asyncio.run(worker.tick()) is True
    # a new worker with a fresh local database: the checkpoint comes down before the crawl
    fresh = Worker(settings.__class__(**{**settings.__dict__, "db_path": str(tmp_path / "fresh.db")}),
                   name="test:2", version="deadbeef", transport=gql.transport,
                   clock=lambda: datetime(2026, 10, 18, 0, 0, 5, tzinfo=UTC))
    assert asyncio.run(fresh.tick()) is True
    with Store(str(tmp_path / "fresh.db")) as local:
        assert local.get_crawl_state("gql:o/r:forks") is not None
    assert [r[0] for r in q(pg_dsn, "select status from gitscout.jobs order by id")] == ["succeeded", "succeeded"]


def test_a_crawl_failure_is_recorded_and_retried(env, pg_dsn):
    settings, worker, gql = env
    gql.queue.pop("Forks", None)
    gql.add_status("Forks", 401)                                   # credentials refused: fatal, no retry loop
    assert asyncio.run(worker.tick()) is True
    rows = q(pg_dsn, "select id, status, trigger, parent_job_id, error, run_after > now() from gitscout.jobs order by id")
    first, retry = rows
    assert first[1] == "failed" and "Bad credentials" in first[4] and SECRET not in first[4]
    assert (retry[1], retry[2], retry[3], retry[5]) == ("queued", "retry", first[0], True)   # backed off, not immediate
    assert [a[0] for a in q(pg_dsn, "select action from gitscout.audit_log order by id")] == ["job.failed"]


def test_shutdown_during_a_job_fails_it_with_a_reason(env, pg_dsn):
    settings, worker, gql = env
    import httpx

    async def slow(request):                      # the fake GitHub answers instantly; give the
        await asyncio.sleep(0.05)                 # cancellation an await to land on
        return gql.handler(request)

    worker.transport = httpx.MockTransport(slow)

    async def go():
        with JobStore(pg_dsn) as store:
            job_id = store.enqueue("o/r", "full", "manual")
            job = store.claim("test:1", "v")
            store.commit()
            cfg = next(r for r in store.repositories() if r.repo == "o/r")
            task = asyncio.create_task(worker.run_job(store, job, cfg))
            await asyncio.sleep(0.02)
            task.cancel()
            outcome = await task
            return job_id, outcome

    job_id, outcome = asyncio.run(go())
    assert isinstance(outcome, Outcome) and outcome.status == "failed" and "shutting down" in outcome.error


def test_serve_once_runs_a_single_tick_and_stops(env, pg_dsn):
    settings, worker, gql = env
    asyncio.run(worker.serve(once=True))
    assert worker.jobs_run == 1


def test_worker_refuses_to_start_without_credentials_or_queue(tmp_path):
    with pytest.raises(JobError, match="SUPABASE_DB_URL"):
        Worker(Settings(tokens=("t",), db_path=str(tmp_path / "x.db")))
    with pytest.raises(JobError, match="GitHub credentials"):
        Worker(Settings(db_path=str(tmp_path / "x.db"), supabase_db_url="postgresql://u:p@h/db"))


def test_restore_checkpoint_only_fills_gaps(tmp_path):
    with Store(str(tmp_path / "c.db")) as local:
        local.set_crawl_state("gql:o/r:forks", cursor="local-newer", done=False)
        n = restore_checkpoint(local, "o/r", {
            "gql:o/r:forks": {"cursor": "old", "done": True, "high_water": None, "total_count": 1},
            "threads:o/r:issues": {"cursor": "c1", "done": False, "high_water": "2026-01-01", "total_count": 9},
            "threads:x/y:issues": {"cursor": "other", "done": False, "high_water": None, "total_count": None},
        })
        assert n == 1
        assert local.get_crawl_state("gql:o/r:forks")["cursor"] == "local-newer"
        assert local.get_crawl_state("threads:o/r:issues") == {"cursor": "c1", "done": False, "high_water": "2026-01-01", "total_count": 9}
        assert local.get_crawl_state("threads:x/y:issues") is None


def test_collections_translate_to_engine_kinds():
    assert engine_kinds(["issues", "prs", "discussions", "forks", "commits"]) == ["issues", "prs", "discussions", "forks", "contribs"]
    assert engine_kinds(["commits", "bogus", "stars"]) == ["contribs", "stars"]
