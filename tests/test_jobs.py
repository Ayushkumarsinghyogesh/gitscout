"""The job queue: cron evaluation, backoff, sanitizing, and the store against a real
Postgres (an embedded server started once per session; skipped if unavailable)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gitscout import jobs
from gitscout.jobs import JobError, JobStore, backoff_seconds, cron_matches, sanitize

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ pure logic


@pytest.mark.parametrize(
    "expr, when, expected",
    [
        ("0 0 * * *", datetime(2026, 10, 16, 0, 0, tzinfo=UTC), True),
        ("0 0 * * *", datetime(2026, 10, 16, 0, 1, tzinfo=UTC), False),
        ("7 9,21 * * *", datetime(2026, 10, 16, 21, 7, tzinfo=UTC), True),
        ("7 9,21 * * *", datetime(2026, 10, 16, 15, 7, tzinfo=UTC), False),
        ("*/15 * * * *", datetime(2026, 10, 16, 3, 45, tzinfo=UTC), True),
        ("*/15 * * * *", datetime(2026, 10, 16, 3, 50, tzinfo=UTC), False),
        ("0 2-4 * * *", datetime(2026, 10, 16, 3, 0, tzinfo=UTC), True),
        ("0 2-4 * * *", datetime(2026, 10, 16, 5, 0, tzinfo=UTC), False),
        ("0 6 * * 1", datetime(2026, 10, 19, 6, 0, tzinfo=UTC), True),   # a Monday
        ("0 6 * * 1", datetime(2026, 10, 18, 6, 0, tzinfo=UTC), False),  # Sunday
        ("0 6 * * 7", datetime(2026, 10, 18, 6, 0, tzinfo=UTC), True),   # 7 is Sunday too
        ("0 6 * * 0", datetime(2026, 10, 18, 6, 0, tzinfo=UTC), True),
        ("0 0 1 * *", datetime(2026, 11, 1, 0, 0, tzinfo=UTC), True),
        ("0 0 1 * *", datetime(2026, 11, 2, 0, 0, tzinfo=UTC), False),
        # both day fields restricted: either one matching is enough, as in cron
        ("0 0 15 * 1", datetime(2026, 10, 19, 0, 0, tzinfo=UTC), True),  # Monday, not the 15th
        ("0 0 15 * 1", datetime(2026, 10, 15, 0, 0, tzinfo=UTC), True),  # the 15th, a Thursday
        ("0 0 15 * 1", datetime(2026, 10, 16, 0, 0, tzinfo=UTC), False),
    ],
)
def test_cron_matches(expr, when, expected):
    assert cron_matches(expr, when) is expected


@pytest.mark.parametrize("expr", ["0 0 * *", "60 0 * * *", "0 24 * * *", "a b c d e", "0 0 0 * *", "*/0 * * * *"])
def test_bad_cron_is_rejected(expr):
    with pytest.raises(JobError):
        cron_matches(expr, datetime(2026, 1, 1, tzinfo=UTC))


def test_backoff_grows_with_jitter_and_caps():
    low = [backoff_seconds(n, rng=lambda: 0.0) for n in (1, 2, 3, 10)]
    high = [backoff_seconds(n, rng=lambda: 1.0) for n in (1, 2, 3, 10)]
    assert low == [30.0, 60.0, 120.0, 900.0]       # half the window
    assert high == [60.0, 120.0, 240.0, 1800.0]    # the whole window, capped at 30 min
    assert 30.0 <= backoff_seconds(1) <= 60.0


def test_sanitize_hides_passwords_and_tokens():
    msg = ("connection to postgresql://gitscout_writer.ref:S3cret%40x@host:6543/postgres failed; "
           "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 rejected; key sb_secret_abc_123")
    out = sanitize(msg)
    assert "S3cret" not in out and "ghp_abcdefghijklmnopqrstuvwxyz" not in out and "sb_secret_abc" not in out
    assert "gitscout_writer.ref:***@host" in out and out.count("***") == 3
    assert sanitize(None) is None
    assert len(sanitize("x" * 5000)) == 2000


def test_code_version_is_short_and_prefers_the_host_variable(monkeypatch):
    monkeypatch.setenv("GITSCOUT_VERSION", "abcdef1234567890")
    assert jobs.code_version() == "abcdef123456"
    monkeypatch.delenv("GITSCOUT_VERSION")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef")
    assert jobs.code_version() == "0123456789ab"


# ------------------------------------------------------------- real Postgres


@pytest.fixture
def store(pg_dsn):
    """A fresh transaction per test, rolled back at the end so tests stay independent."""
    s = JobStore(pg_dsn, clock=lambda: datetime.now(UTC), rng=lambda: 0.0)
    s.__enter__()
    s._rows("delete from gitscout.jobs")
    s._rows("update gitscout.repositories set last_scheduled_at = null, last_success_at = null, "
            "last_job_id = null, checkpoint = '{}', enabled = true, schedule_cron = '0 0 * * *', "
            "timezone = 'Asia/Kolkata'")
    s.commit()
    yield s
    s._conn.rollback()
    s._rows("delete from gitscout.jobs")
    s.commit()
    s.__exit__(None, None, None)


def test_enqueue_refuses_a_second_active_job_for_the_same_repo_and_kind(store):
    first = store.enqueue("o/r", "incremental", "schedule")
    assert isinstance(first, int)
    assert store.enqueue("o/r", "incremental", "manual") is None       # queued already
    assert store.enqueue("o/r", "full", "manual") is not None           # another kind is fine
    assert store.enqueue("o/s", "incremental", "schedule") is not None  # another repo is fine
    assert store.get(first).status == "queued"


def test_unknown_kind_or_trigger_is_rejected(store):
    with pytest.raises(JobError):
        store.enqueue("o/r", "nightly", "schedule")
    with pytest.raises(JobError):
        store.enqueue("o/r", "incremental", "cron")


def test_claim_runs_the_earliest_due_job_and_records_the_worker(store):
    later = store.enqueue("o/r", "full", "manual", priority=200)
    first = store.enqueue("o/r", "incremental", "schedule")
    future = store.enqueue("o/s", "incremental", "schedule",
                           run_after=datetime.now(UTC) + timedelta(hours=1))
    job = store.claim(worker="host:1", version="abc1234")
    assert job.id == first and job.status == "running" and job.attempt == 1
    assert job.worker == "host:1" and job.code_version == "abc1234" and job.started_at is not None
    assert store.claim().id == later                   # lower priority number first, then run_after
    assert store.claim() is None                       # the future job is not due
    assert store.get(future).status == "queued"


def test_two_workers_never_claim_the_same_job(pg_dsn, store):
    a = store.enqueue("o/r", "incremental", "schedule")
    b = store.enqueue("o/s", "incremental", "schedule")
    store.commit()
    other = JobStore(pg_dsn)
    with other:
        mine = store.claim(worker="w1")          # holds the row lock, not committed
        theirs = other.claim(worker="w2")        # must skip the locked row, not block
        assert {mine.id, theirs.id} == {a, b}
    store.commit()
    assert {j.status for j in store.list()} == {"running"}


def test_heartbeat_stores_progress_and_reports_cancellation(store):
    job_id = store.enqueue("o/r", "incremental", "schedule")
    store.claim()
    assert store.heartbeat(job_id, counters={"issues": {"discovered": 5}}) == "running"
    assert store.get(job_id).counters == {"issues": {"discovered": 5}}
    assert store.cancel(job_id) is True
    assert store.heartbeat(job_id) == "cancelled"      # the worker sees it and stops
    with pytest.raises(JobError):
        store.heartbeat(999999)


def test_success_closes_the_job_and_updates_the_repository(store):
    job_id = store.enqueue("o/r", "incremental", "schedule")
    store.claim()
    assert store.finish(job_id, "succeeded", counters={"issues": {"inserted": 2}},
                        checkpoint={"issues": "cursor-x"}, rate={"points_used": 12}) is None
    job = store.get(job_id)
    assert job.status == "succeeded" and job.finished_at is not None and job.rate == {"points_used": 12}
    repo = next(r for r in store.repositories() if r.repo == "o/r")
    assert repo.last_success_at is not None and repo.last_job_id == job_id
    assert repo.checkpoint == {"issues": "cursor-x"}


def test_failure_enqueues_a_backed_off_retry_until_attempts_run_out(store):
    job_id = store.enqueue("o/r", "incremental", "schedule", max_attempts=2)
    store.claim()
    retry_id = store.finish(job_id, "failed", error="GitHub error: postgresql://u:pw@h/db boom")
    failed = store.get(job_id)
    assert failed.status == "failed" and "pw@" not in failed.error and "***@h" in failed.error
    retry = store.get(retry_id)
    assert retry.trigger == "retry" and retry.parent_job_id == job_id and retry.attempt == 1
    assert retry.run_after > datetime.now(UTC) + timedelta(seconds=20)   # backoff: 30 s at rng=0
    assert store.claim() is None                                          # not due yet
    store._rows("update gitscout.jobs set run_after = now() where id = %s", [retry_id])
    assert store.claim().id == retry_id
    assert store.get(retry_id).attempt == 2
    assert store.finish(retry_id, "failed", error="again") is None        # attempts exhausted
    assert [j.status for j in store.list(repo="o/r")] == ["failed", "failed"]


def test_finish_requires_a_running_job_and_a_final_status(store):
    job_id = store.enqueue("o/r", "incremental", "schedule")
    with pytest.raises(JobError):
        store.finish(job_id, "succeeded")          # still queued
    store.claim()
    with pytest.raises(JobError):
        store.finish(job_id, "running")


def test_stale_running_jobs_are_failed_and_retried(store):
    job_id = store.enqueue("o/r", "incremental", "schedule")
    store.claim()
    assert store.requeue_stale(timedelta(minutes=10)) == []              # fresh heartbeat
    store._rows("update gitscout.jobs set heartbeat_at = now() - interval '11 minutes' where id = %s", [job_id])
    (stale, retry), = store.requeue_stale(timedelta(minutes=10))
    assert stale == job_id and store.get(job_id).status == "failed"
    assert "stalled" in store.get(job_id).error and store.get(retry).status == "queued"


def test_cancel_only_touches_active_jobs(store):
    job_id = store.enqueue("o/r", "incremental", "schedule")
    assert store.cancel(job_id) is True and store.get(job_id).status == "cancelled"
    assert store.cancel(job_id) is False
    assert store.enqueue("o/r", "incremental", "schedule") is not None   # the slot is free again


def test_due_follows_each_repository_clock_and_fires_once_per_minute(store):
    store.set_repository("o/r", schedule_cron="30 2 * * *", tz="Asia/Kolkata")  # 02:30 IST = 21:00 UTC
    store.set_repository("o/s", schedule_cron="0 0 * * *", tz="UTC")
    at = datetime(2026, 10, 16, 21, 0, 15, tzinfo=UTC)
    due = store.due(at)
    assert [(c.repo, m) for c, m in due] == [("o/r", datetime(2026, 10, 16, 21, 0, tzinfo=UTC))]
    store.mark_scheduled("o/r", due[0][1])
    assert store.due(at + timedelta(seconds=40)) == []                   # same minute: not again
    assert store.due(datetime(2026, 10, 17, 0, 0, tzinfo=UTC))[0][0].repo == "o/s"
    store.set_repository("o/s", enabled=False)
    assert store.due(datetime(2026, 10, 17, 0, 0, tzinfo=UTC)) == []     # disabled: skipped


def test_a_repository_with_a_broken_timezone_is_skipped_not_fatal(store):
    store._rows("update gitscout.repositories set timezone = 'Mars/Olympus' where repo = 'o/r'")
    store.set_repository("o/s", schedule_cron="0 0 * * *", tz="UTC")
    assert [c.repo for c, _ in store.due(datetime(2026, 10, 17, 0, 0, tzinfo=UTC))] == ["o/s"]


def test_set_repository_validates_and_reports_unknown_repos(store):
    with pytest.raises(JobError):
        store.set_repository("o/r", schedule_cron="every night")
    with pytest.raises(JobError):
        store.set_repository("o/r", tz="Nowhere/Land")
    assert store.set_repository("o/nope", enabled=False) is False
    assert store.set_repository("o/r", collections=["issues", "prs"], settings={"max": 10}) is True
    cfg = next(r for r in store.repositories() if r.repo == "o/r")
    assert cfg.collections == ["issues", "prs"] and cfg.settings == {"max": 10}


def test_audit_rows_and_settings(store):
    store.audit("job.enqueue", actor_email="a@b.c", repo="o/r", target_kind="job", target_id="1",
                details={"kind": "full"})
    (row,) = store._rows("select action, actor_email, details->>'kind' from gitscout.audit_log order by id desc limit 1")
    assert row == ("job.enqueue", "a@b.c", "full")
    assert store.settings()["stale_job_minutes"] == 10
