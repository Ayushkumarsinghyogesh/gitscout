"""The cron layer: interval parsing, the watch loop, and crash tolerance."""
from __future__ import annotations

import asyncio

import pytest
from gqlhelpers import authored_body, gql_user

from gitscout.config import Settings
from gitscout.scheduler import (
    MIN_INTERVAL,
    format_interval,
    parse_interval,
    run_log,
    watch,
)
from gitscout.targets import targets_from_repos


def run(coro):
    return asyncio.run(coro)


TARGETS = targets_from_repos(["o/r"], ("issues",))


def test_parse_interval_units():
    assert parse_interval("90s") == 90
    assert parse_interval("15m") == 900
    assert parse_interval("6h") == 21600
    assert parse_interval("1d") == 86400
    assert parse_interval("1.5h") == 5400


def test_a_bare_number_means_minutes():
    assert parse_interval("30") == 1800
    assert parse_interval(30) == 1800


def test_intervals_have_a_floor_and_must_parse():
    with pytest.raises(ValueError, match="at least"):
        parse_interval("5s")
    assert parse_interval(f"{int(MIN_INTERVAL)}s") == MIN_INTERVAL
    for bad in ("", "soon", "6 weeks", "-5m"):
        with pytest.raises(ValueError):
            parse_interval(bad)


def test_format_interval_is_the_inverse_for_round_values():
    for text in ("90s", "15m", "6h", "1d"):
        assert format_interval(parse_interval(text)) == text


# -------------------------------------------------------------------- the loop


def _settings(tmp_path):
    return Settings(tokens=("tok",), db_path=str(tmp_path / "w.db"))


def test_watch_runs_the_requested_number_of_times(gql, tmp_path, monkeypatch):
    gql.add("Issues", authored_body("issues", [(gql_user("alice", email="a@acme.dev"), "2024-06-01T00:00:00Z")]))
    gql.add("CommitEmails", {})
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    async def go():
        return await watch(
            _settings(tmp_path),
            TARGETS,
            every="15m",
            max_runs=2,
            jitter=0,
            sleep=no_sleep,
            transport=gql.transport,
        )

    # the loop waits on an Event with a timeout, so patch that rather than sleep
    state = run(_with_fast_waits(go()))
    assert state.runs == 2 and state.failures == 0
    assert state.interactions_new == 1  # the second run is incremental: nothing new
    assert state.emails_new == 1


async def _with_fast_waits(coro):
    """Make asyncio.wait return immediately so the loop does not really sleep."""
    real_wait = asyncio.wait

    async def fast_wait(aws, **kw):
        return await real_wait(aws, timeout=0)

    asyncio.wait = fast_wait  # type: ignore[assignment]
    try:
        return await coro
    finally:
        asyncio.wait = real_wait  # type: ignore[assignment]


def test_watch_keeps_going_after_a_failing_run(tmp_path):
    """One transient GitHub error must not kill an unattended scraper."""

    async def go():
        return await watch(
            _settings(tmp_path),
            TARGETS,
            every="15m",
            max_runs=2,
            jitter=0,
            transport=_always_fatal(),
        )

    state = run(_with_fast_waits(go()))
    assert state.runs == 0 and state.failures == 2
    assert state.last_error and "GraphQL" in state.last_error


def _always_fatal():
    """400 is fatal rather than retryable, so the test does not wait out the backoff."""
    import httpx

    return httpx.MockTransport(lambda request: httpx.Response(400, json={"message": "boom"}))


def test_watch_stops_when_the_stop_event_is_set(gql, tmp_path):
    gql.add("Issues", authored_body("issues", [(gql_user("a"), "2024-06-01T00:00:00Z")]))
    gql.add("CommitEmails", {})

    async def go():
        stop = asyncio.Event()

        def after_first(result):
            stop.set()

        return await watch(
            _settings(tmp_path),
            TARGETS,
            every="1h",
            jitter=0,
            stop=stop,
            on_result=after_first,
            transport=gql.transport,
        )

    state = run(go())
    assert state.runs == 1


def test_watch_records_every_run_in_the_audit_log(gql, tmp_path):
    gql.add("Issues", authored_body("issues", [(gql_user("a"), "2024-06-01T00:00:00Z")]))
    gql.add("CommitEmails", {})
    settings = _settings(tmp_path)

    async def go():
        return await watch(
            settings, TARGETS, every="15m", max_runs=2, jitter=0, transport=gql.transport
        )

    run(_with_fast_waits(go()))
    rows = run_log(settings)
    assert len(rows) == 2
    assert all(r["mode"] == "watch" and r["status"] == "ok" for r in rows)


def test_watch_exports_only_this_run_s_finds(gql, tmp_path):
    import csv

    gql.add("Issues", authored_body("issues", [(gql_user("alice", email="a@acme.dev"), "2024-06-01T00:00:00Z")]))
    gql.add("CommitEmails", {})
    out = tmp_path / "new.csv"

    async def go():
        return await watch(
            _settings(tmp_path),
            TARGETS,
            every="15m",
            max_runs=2,
            jitter=0,
            outputs=[out],
            new_only=True,
            transport=gql.transport,
        )

    run(_with_fast_waits(go()))
    # run 1 found alice; run 2 found nobody, so the file it wrote is empty
    assert list(csv.DictReader(out.open(encoding="utf-8"))) == []


def test_watch_can_export_everything_instead(gql, tmp_path):
    import csv

    gql.add("Issues", authored_body("issues", [(gql_user("alice", email="a@acme.dev"), "2024-06-01T00:00:00Z")]))
    gql.add("CommitEmails", {})
    out = tmp_path / "all.csv"

    async def go():
        return await watch(
            _settings(tmp_path),
            TARGETS,
            every="15m",
            max_runs=2,
            jitter=0,
            outputs=[out],
            new_only=False,
            transport=gql.transport,
        )

    run(_with_fast_waits(go()))
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["login"] for r in rows] == ["alice"]
