"""Phase 6 observability: alert rules, cooldown, Resend delivery, worker liveness,
repository health, the alerts a real worker tick raises, and JSON logs."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest
from typer.testing import CliRunner

from gitscout import cli
from gitscout.alerts import Alert, Alerter, for_finished_job
from gitscout.config import Settings
from gitscout.jobs import Job, JobStore
from gitscout.logs import JobContextFilter, JsonFormatter, job_context
from gitscout.worker import Worker
from test_worker import SECRET, env  # noqa: F401 - the worker fixture

UTC = timezone.utc
NOW = datetime(2026, 10, 17, 0, 0, 20, tzinfo=UTC)


def job(**kw) -> Job:
    base = dict(id=9, repo="o/r", kind="incremental", trigger="schedule", status="running", attempt=3,
                max_attempts=3, run_after=NOW, created_at=NOW)
    return Job(**{**base, **kw})


class Resend:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(self.status, json={"id": "e_1"} if self.status < 300 else {"message": "bad"})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def q(dsn, sql, params=()):
    with psycopg.connect(dsn, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def clean(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("delete from gitscout.alerts; delete from gitscout.workers; delete from gitscout.jobs; "
                  "delete from gitscout.audit_log")
        c.execute("update gitscout.app_settings set value = '[\"ops@example.com\"]' where key = 'alert_emails'")
        c.execute("update gitscout.repositories set enabled = false, last_success_at = null, added_at = now()")
    yield pg_dsn
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("update gitscout.app_settings set value = '[]' where key = 'alert_emails'")


# ------------------------------------------------------------------- rules


def test_a_final_failure_is_critical_and_an_auth_error_is_flagged_even_with_retries():
    final = for_finished_job(job(), "failed", retry=None, error="RuntimeError: boom", counters={}, rate={})
    assert [(a.key, a.severity) for a in final] == [("job_failed:o/r:incremental", "critical")]
    assert "gitscout jobs show 9" in final[0].body
    auth = for_finished_job(job(attempt=1), "failed", retry=10, error="GitHubAuthError: Bad credentials",
                            counters={}, rate={})
    assert [a.key for a in auth] == ["auth"]                          # a retry is queued: no job_failed yet
    assert for_finished_job(job(attempt=1), "failed", retry=10, error="timeout", counters={}, rate={}) == []


def test_success_warns_on_a_low_rate_budget_and_a_failed_refresh():
    alerts = for_finished_job(job(), "succeeded", retry=None, error=None,
                              counters={"push": {"refresh_error": "canceling statement"}},
                              rate={"remaining": 40, "reset_at": "2026-10-17T01:00:00+00:00"}, rate_floor=250)
    assert [a.key for a in alerts] == ["rate:o/r", "refresh:o/r"]
    assert for_finished_job(job(), "succeeded", retry=None, error=None, counters={},
                            rate={"remaining": 4000}) == []


# ------------------------------------------------------------- delivery


def test_alerts_are_emailed_once_per_cooldown_and_always_recorded(clean):
    resend = Resend()
    settings = Settings(resend_api_key="re_test_key", supabase_db_url=clean)
    alerter = Alerter(settings, transport=resend.transport, clock=lambda: datetime.now(UTC))
    with JobStore(clean) as store:
        first = alerter.raise_all(store, [Alert("k1", "warning", "subject", "body", "o/r", 1)])
        again = alerter.raise_all(store, [Alert("k1", "warning", "subject", "body", "o/r", 2)])
    assert len(first) == 1 and again == []                            # cooled down: no second row, no mail
    (auth, payload), = resend.calls
    assert auth == "Bearer re_test_key" and payload["to"] == ["ops@example.com"]
    assert payload["subject"] == "[GitScout warning] subject"
    assert q(clean, "select key, channel, sent_at is not null, error from gitscout.alerts") == [("k1", "email", True, None)]


def test_without_a_key_or_with_a_refusing_provider_the_alert_still_lands(clean):
    with JobStore(clean) as store:
        Alerter(Settings(supabase_db_url=clean)).raise_all(store, [Alert("nokey", "info", "s", "b")])
        Alerter(Settings(resend_api_key="re_x", supabase_db_url=clean), transport=Resend(500).transport) \
            .raise_all(store, [Alert("refused", "info", "s", "b")])
    rows = dict((r[0], r[1:]) for r in q(clean, "select key, channel, sent_at, error from gitscout.alerts"))
    assert rows["nokey"] == ("none", None, "RESEND_API_KEY is not set")
    assert rows["refused"][0] == "email" and rows["refused"][1] is None and "500" in rows["refused"][2]


def test_secrets_never_reach_the_alert_row(clean):
    with JobStore(clean) as store:
        Alerter(Settings(supabase_db_url=clean)).raise_all(store, [Alert("s", "info", "s", f"token {SECRET} leaked")])
    assert SECRET not in q(clean, "select body from gitscout.alerts")[0][0]


# ---------------------------------------------------------------- health


def test_overdue_repositories_alert_and_show_in_health(clean):
    q(clean, "update gitscout.repositories set enabled = true, added_at = now() - interval '30 hours' where repo = 'o/r'")
    with JobStore(clean) as store:
        ids = Alerter(Settings(supabase_db_url=clean)).check_overdue(store)
        health = {r["repo"]: r for r in store.health()}
    assert len(ids) == 1 and health["o/r"]["overdue"] is True and health["o/s"]["overdue"] is False
    assert q(clean, "select key from gitscout.alerts") == [("overdue:o/r",)]


def test_worker_liveness_is_upserted(clean):
    with JobStore(clean) as store:
        assert store.seen("h:1", "abc", jobs_run=2, current_job_id=5) is True
        store.seen("h:1", "abc", jobs_run=3)
        (w,) = store.workers()
    assert (w["name"], w["jobs_run"], w["current_job_id"], w["stopped_at"]) == ("h:1", 3, None, None)
    assert w["silent_seconds"] <= 5


def test_health_command_exits_1_when_no_worker_is_alive(clean, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_DB_URL", clean)
    monkeypatch.setenv("GITSCOUT_DB", str(tmp_path / "h.db"))
    result = CliRunner().invoke(cli.app, ["health"])
    assert result.exit_code == 1 and "no worker seen" in result.output
    with JobStore(clean) as store:
        store.seen("h:2", "abc")
    result = CliRunner().invoke(cli.app, ["health"])
    assert result.exit_code == 0, result.output
    assert "h:2" in result.output and "alive" in result.output


# ------------------------------------------------- through a worker tick


def test_an_exhausted_job_alerts_and_the_next_success_says_recovered(env, clean):  # noqa: F811
    settings, worker, gql = env
    resend = Resend()
    worker.settings = Settings(**{**settings.__dict__, "resend_api_key": "re_k"})
    worker.alerter = Alerter(worker.settings, transport=resend.transport, clock=lambda: datetime.now(UTC))
    q(clean, "update gitscout.repositories set enabled = true, collections = '{forks}', timezone = 'UTC', "
             "schedule_cron = '0 0 * * *', added_at = now() where repo = 'o/r'")
    with JobStore(clean) as store:
        store.enqueue("o/r", "incremental", "manual", max_attempts=1)
        q(clean, "update gitscout.repositories set last_scheduled_at = '2026-10-17T00:00:00Z' where repo = 'o/r'")
    gql.queue.pop("Forks", None)
    gql.add_status("Forks", 401)
    assert asyncio.run(worker.tick()) is True
    keys = [r[0] for r in q(clean, "select key from gitscout.alerts order by id")]
    assert keys == ["auth", "job_failed:o/r:incremental"]
    assert [c[1]["subject"] for c in resend.calls][0].startswith("[GitScout critical]")
    (seen,) = q(clean, "select jobs_run from gitscout.workers")
    assert seen == (1,)
    # GitHub answers again; the next run succeeds and the failure is closed out
    gql.queue.pop("Forks", None)
    from gqlhelpers import forks_body, gql_user
    gql.add("Forks", forks_body([(gql_user("carol", name="Carol C"), "2024-03-01T00:00:00Z")]))
    with JobStore(clean) as store:
        store.enqueue("o/r", "incremental", "manual")
    assert asyncio.run(worker.tick()) is True
    keys = [r[0] for r in q(clean, "select key from gitscout.alerts order by id")]
    assert keys[-1] == "recovered:o/r:incremental"


def test_a_stalled_job_alerts(env, clean):  # noqa: F811
    settings, worker, gql = env
    with JobStore(clean) as store:
        store.enqueue("o/r", "full", "manual")
        store.claim("dead:1", "v")
    q(clean, "update gitscout.jobs set heartbeat_at = now() - interval '1 hour'")
    q(clean, "update gitscout.repositories set enabled = false")
    asyncio.run(worker.tick())
    assert ("stalled:o/r",) in q(clean, "select key from gitscout.alerts")


# ------------------------------------------------------------------- logs


def test_json_logs_carry_the_job_and_hide_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(JobContextFilter())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("gitscout.test.logs")
    logger.addHandler(handler)
    logger.propagate = False
    token = job_context.set({"job_id": 42, "repo": "o/r", "kind": "incremental"})
    try:
        logger.warning("using %s on postgresql://u:hunter2@h/db", SECRET)
    finally:
        job_context.reset(token)
        logger.removeHandler(handler)
    line = json.loads(stream.getvalue())
    assert (line["job_id"], line["repo"], line["kind"], line["level"]) == (42, "o/r", "incremental", "warning")
    assert SECRET not in line["msg"] and "hunter2" not in line["msg"]
