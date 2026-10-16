"""`gitscout jobs ...` and `gitscout repos ...` against a fake store: wiring, exit codes,
audit rows -- the store itself is covered by test_jobs.py on a real Postgres."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from gitscout import cli, jobs
from gitscout.jobs import Job, JobError, RepoConfig

runner = CliRunner()
UTC = timezone.utc


class FakeStore:
    instances: list["FakeStore"] = []

    def __init__(self, dsn, schema="gitscout"):
        self.dsn, self.schema = dsn, schema
        self.audits: list[tuple] = []
        self.jobs = {
            7: Job(id=7, repo="o/r", kind="incremental", trigger="schedule", status="failed", attempt=3,
                   max_attempts=3, run_after=datetime(2026, 10, 16, 18, 30, tzinfo=UTC),
                   created_at=datetime(2026, 10, 16, 18, 30, tzinfo=UTC),
                   finished_at=datetime(2026, 10, 16, 18, 35, tzinfo=UTC),
                   error="GitHub error: 502\nsecond line"),
        }
        self.repos = [RepoConfig("o/r", True, ["issues", "prs"], "0 0 * * *", "Asia/Kolkata")]
        self.cancelled: list[int] = []
        FakeStore.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def list(self, repo=None, status=None, limit=50):
        return [j for j in self.jobs.values() if (not repo or j.repo == repo) and (not status or j.status == status)][:limit]

    def enqueue(self, repo, kind, trigger, priority=100, **_):
        if kind not in jobs.KINDS:
            raise JobError(f"unknown job kind {kind!r}")
        if repo == "o/busy":
            return None
        return 42

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return job_id == 7

    def requeue_stale(self, older_than):
        return [(7, 8)] if older_than.total_seconds() == 600 else []

    def repositories(self, enabled_only=False):
        return self.repos

    def set_repository(self, repo, **changes):
        if changes.get("schedule_cron") == "bad":
            raise JobError("cron needs 5 fields")
        self.changes = changes
        return repo == "o/r"

    def audit(self, action, **fields):
        self.audits.append((action, fields))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    FakeStore.instances = []
    monkeypatch.setattr(jobs, "JobStore", FakeStore)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://gitscout_writer.ref:x@host:6543/postgres")
    monkeypatch.setenv("GITHUB_TOKENS", "tok")
    monkeypatch.setenv("GITSCOUT_DB", str(tmp_path / "t.db"))
    return FakeStore


def test_jobs_list_shows_first_error_line(fake):
    result = runner.invoke(cli.app, ["jobs", "list", "--repo", "o/r"])
    assert result.exit_code == 0, result.output
    assert "o/r" in result.output and "failed" in result.output and "3/3" in result.output
    assert "GitHub error: 502" in result.output and "second line" not in result.output
    assert runner.invoke(cli.app, ["jobs", "list", "--status", "queued"]).output.strip() == "no jobs"


def test_jobs_enqueue_audits_and_refuses_duplicates(fake):
    ok = runner.invoke(cli.app, ["jobs", "enqueue", "o/r", "--kind", "full"])
    assert ok.exit_code == 0 and "queued job 42" in ok.output
    assert fake.instances[-1].audits == [("job.enqueue", {"actor_email": "cli", "repo": "o/r", "target_kind": "job",
                                                          "target_id": "42", "details": {"kind": "full"}})]
    busy = runner.invoke(cli.app, ["jobs", "enqueue", "o/busy"])
    assert busy.exit_code == 3 and "already has" in busy.output
    bad = runner.invoke(cli.app, ["jobs", "enqueue", "o/r", "--kind", "hourly"])
    assert bad.exit_code == 2 and "unknown job kind" in bad.output


def test_jobs_cancel_and_requeue_stale(fake):
    assert runner.invoke(cli.app, ["jobs", "cancel", "7"]).exit_code == 0
    assert runner.invoke(cli.app, ["jobs", "cancel", "8"]).exit_code == 1
    out = runner.invoke(cli.app, ["jobs", "requeue-stale", "--minutes", "10"])
    assert out.exit_code == 0 and "job 7 failed as stalled; retry 8" in out.output
    assert "no stalled jobs" in runner.invoke(cli.app, ["jobs", "requeue-stale", "--minutes", "5"]).output


def test_repos_list_and_set(fake):
    listing = runner.invoke(cli.app, ["repos", "list"])
    assert listing.exit_code == 0 and "0 0 * * *" in listing.output and "issues,prs" in listing.output
    ok = runner.invoke(cli.app, ["repos", "set", "o/r", "--disable", "--schedule", "30 1 * * *",
                                 "--tz", "UTC", "--collections", "issues, prs", "--max", "500"])
    assert ok.exit_code == 0, ok.output
    assert fake.instances[-1].changes == {"enabled": False, "schedule_cron": "30 1 * * *", "tz": "UTC",
                                          "collections": ["issues", "prs"], "settings": {"max": 500}}
    assert fake.instances[-1].audits[-1][0] == "repo.configure"
    assert runner.invoke(cli.app, ["repos", "set", "o/nope", "--enable"]).exit_code == 1
    bad = runner.invoke(cli.app, ["repos", "set", "o/r", "--schedule", "bad"])
    assert bad.exit_code == 2 and "cron needs 5 fields" in bad.output


def test_queue_commands_need_a_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("GITHUB_TOKENS", "tok")
    monkeypatch.setenv("GITSCOUT_DB", str(tmp_path / "t.db"))
    result = runner.invoke(cli.app, ["jobs", "list"])
    assert result.exit_code == 2 and "SUPABASE_DB_URL" in result.output
