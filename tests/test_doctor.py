"""Doctor: the preflight that proves every query still matches the live schema."""
from __future__ import annotations

import asyncio

import httpx
from gqlhelpers import (
    DEFAULT_RATE_LIMIT,
    authored_body,
    commit_probe,
    connection,
    contribs_body,
    error,
    forks_body,
    gql_user,
    op_name,
    repo_commit,
    stars_body,
)

from gitscout.config import Settings
from gitscout.doctor import FAIL, OK, WARN, run_doctor, targets_report
from gitscout.queries import KIND_QUERIES


def run(coro):
    return asyncio.run(coro)


def status_of(report, name):
    return next(c.status for c in report.checks if c.name == name)


def names(report):
    return [c.name for c in report.checks]


# ------------------------------------------------------------------- offline


def test_offline_report_flags_a_missing_token(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=False))
    assert status_of(report, "github token") == FAIL
    assert status_of(report, "database") == OK
    assert status_of(report, "live checks") == WARN
    assert not report.ok


def test_offline_report_passes_with_a_token(tmp_path):
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=False))
    assert status_of(report, "github token") == OK
    assert "5000 GraphQL points" in next(
        c.detail for c in report.checks if c.name == "github token"
    )


def test_live_checks_are_skipped_without_a_token(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True))
    assert status_of(report, "live checks") == WARN
    assert "graphql auth" not in names(report)


def test_unreadable_database_fails(tmp_path):
    # a directory cannot be opened as a SQLite file
    settings = Settings(tokens=("t",), db_path=str(tmp_path))
    report = run(run_doctor(settings, live=False))
    assert status_of(report, "database") == FAIL


def test_report_lines_are_aligned_and_prefixed(tmp_path):
    report = run(run_doctor(Settings(db_path=str(tmp_path / "t.db")), live=False))
    lines = report.lines()
    assert all(line.startswith(("[PASS]", "[WARN]", "[FAIL]")) for line in lines)


# ---------------------------------------------------------------------- live


def _healthy_transport():
    """A fake GitHub answering every query doctor sends."""
    bodies = {
        "Viewer": {"viewer": {"login": "tester"}},
        "Stars": stars_body([(gql_user("a"), "2024-01-01T00:00:00Z")], total=7),
        "Forks": forks_body([(gql_user("b"), "2024-01-01T00:00:00Z")]),
        "Issues": authored_body("issues", [(gql_user("c"), "2024-01-01T00:00:00Z")]),
        "PullRequests": authored_body("pullRequests", [(gql_user("d"), "2024-01-01T00:00:00Z")]),
        "Discussions": authored_body("discussions", [(gql_user("e"), "2024-01-01T00:00:00Z")]),
        "Contributors": contribs_body([repo_commit("f@acme.dev", user=gql_user("f"))], total=3),
        "CommitEmails": {"u0": commit_probe("tester", repos=[[]])},
        "Discover": {"search": connection(nodes=[], total=12)},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rate_limit":
            return httpx.Response(
                200, json={"resources": {"core": {"limit": 5000, "remaining": 4999, "reset": 1}}}
            )
        import json

        name = op_name(json.loads(request.content or b"{}").get("query", ""))
        data = dict(bodies.get(name, {}))
        data["rateLimit"] = DEFAULT_RATE_LIMIT
        return httpx.Response(200, json={"data": data})

    return httpx.MockTransport(handler)


def test_healthy_live_report_passes_every_query(tmp_path):
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True, transport=_healthy_transport()))

    assert report.ok, [c for c in report.checks if c.status != OK]
    assert status_of(report, "rest api") == OK
    assert status_of(report, "graphql auth") == OK
    for kind in KIND_QUERIES:
        assert status_of(report, f"query: {kind}") == OK
    assert status_of(report, "query: commit emails") == OK
    assert status_of(report, "query: discover") == OK
    assert "tester" in next(c.detail for c in report.checks if c.name == "graphql auth")
    assert "totalCount=7" in next(c.detail for c in report.checks if c.name == "query: stars")


def test_a_schema_mismatch_is_reported_as_a_failure(tmp_path):
    """This is the check that catches GitHub renaming or removing a field."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rate_limit":
            return httpx.Response(
                200, json={"resources": {"core": {"limit": 5000, "remaining": 1, "reset": 1}}}
            )
        import json

        name = op_name(json.loads(request.content or b"{}").get("query", ""))
        if name == "Stars":
            return httpx.Response(
                200,
                json={
                    "data": None,
                    "errors": [error("Field 'starredAt' doesn't exist on type 'StargazerEdge'")],
                },
            )
        return httpx.Response(200, json={"data": {"rateLimit": DEFAULT_RATE_LIMIT}})

    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True, transport=handler_transport(handler)))
    assert status_of(report, "query: stars") == FAIL
    assert "schema mismatch" in next(c.detail for c in report.checks if c.name == "query: stars")
    assert not report.ok


def handler_transport(handler):
    return httpx.MockTransport(handler)


def test_bad_credentials_fail_loudly(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    settings = Settings(tokens=("nope",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True, transport=handler_transport(handler)))
    assert status_of(report, "rest api") == FAIL


def test_a_missing_probe_repo_is_reported_not_silently_passed(tmp_path):
    """NOT_FOUND must not be mistaken for a healthy query."""

    def handler(request):
        if request.url.path == "/rate_limit":
            return httpx.Response(
                200, json={"resources": {"core": {"limit": 5000, "remaining": 1, "reset": 1}}}
            )
        return httpx.Response(
            200,
            json={
                "data": {"rateLimit": DEFAULT_RATE_LIMIT},
                "errors": [error("Could not resolve to a Repository", type_="NOT_FOUND")],
            },
        )

    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True, transport=handler_transport(handler)))
    assert status_of(report, "graphql auth") == FAIL


# ------------------------------------------------------------------- targets


def test_targets_report_validates_the_shipped_profile():
    report = targets_report(["cloud-security"])
    assert report.ok
    assert "repos" in report.checks[0].detail


def test_targets_report_flags_a_broken_profile(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('name="x"\n[[target]]\nrepo="not a repo"\n', encoding="utf-8")
    report = targets_report([bad])
    assert not report.ok


def test_apify_check_only_runs_when_configured(tmp_path):
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True, transport=_healthy_transport()))
    assert "apify api" not in names(report)

    def apify_handler(request):
        return httpx.Response(200, json={"id": "actor"})

    configured = Settings(
        tokens=("tok",), db_path=str(tmp_path / "t2.db"), apify_token="atok"
    )
    report = run(
        run_doctor(
            configured,
            live=True,
            transport=_healthy_transport(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(apify_handler)),
        )
    )
    assert status_of(report, "apify api") == OK


SCOPE_MSG = (
    "Your token has not been granted the required scopes to execute this query. "
    "The 'email' field requires one of the following scopes: ['user:email', 'read:user']"
)


def test_a_missing_read_user_scope_is_a_warning_not_a_schema_failure(tmp_path):
    """Reproduces a real run: a scopeless token must not look like a broken schema."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rate_limit":
            return httpx.Response(
                200, json={"resources": {"core": {"limit": 5000, "remaining": 4999, "reset": 1}}}
            )
        import json

        body = json.loads(request.content or b"{}")
        query = body.get("query", "")
        name = op_name(query)
        # GitHub only raises this when the query actually asks for `email`
        if "\n  email\n" in query:
            return httpx.Response(
                200, json={"data": None, "errors": [error(SCOPE_MSG)]}
            )
        bodies = {
            "Viewer": {"viewer": {"login": "tester"}},
            "Stars": stars_body([(gql_user("a"), "2024-01-01T00:00:00Z")], total=5),
            "Forks": forks_body([(gql_user("b"), "2024-01-01T00:00:00Z")]),
            "Issues": authored_body("issues", [(gql_user("c"), "2024-01-01T00:00:00Z")]),
            "PullRequests": authored_body("pullRequests", [(gql_user("d"), "2024-01-01T00:00:00Z")]),
            "Discussions": authored_body("discussions", [(gql_user("e"), "2024-01-01T00:00:00Z")]),
            "Contributors": contribs_body([repo_commit("f@acme.dev", user=gql_user("f"))], total=3),
            "CommitEmails": {"u0": commit_probe("tester", repos=[[]])},
            "Discover": {"search": connection(nodes=[], total=12)},
        }
        data = dict(bodies.get(name, {}))
        data["rateLimit"] = DEFAULT_RATE_LIMIT
        return httpx.Response(200, json={"data": data})

    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    report = run(run_doctor(settings, live=True, transport=httpx.MockTransport(handler)))

    # the scope is called out once, in actionable terms
    assert status_of(report, "scope: read:user") == WARN
    detail = next(c.detail for c in report.checks if c.name == "scope: read:user")
    assert "read:user" in detail and "commit emails are unaffected" in detail

    # and every query still passes, because they were retried without the field
    for kind in KIND_QUERIES:
        assert status_of(report, f"query: {kind}") == OK, kind
    # a scope warning alone must not fail the run
    assert report.ok
