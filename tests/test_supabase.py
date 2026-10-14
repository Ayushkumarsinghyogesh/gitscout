"""The Supabase mirror: upsert batching, retries, referential safety, watermarks.

Everything here is mocked. The push must never need a real project to be tested, and
the service key must never appear in an error message.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from gitscout.cli import app
from gitscout.config import Settings
from gitscout.models import EmailCandidate, Interaction, Profile, ThreadComment, ThreadRecord
from gitscout.storage import Store
from gitscout.supabase import (
    SYNC_KEY,
    SupabaseClient,
    SupabaseError,
    collect,
    push,
    schema_sql,
    watermark_of,
)

runner = CliRunner()
KEY = "svc-secret-key"


def run(coro):
    return asyncio.run(coro)


class FakeRest:
    """A scripted PostgREST. Records every request; replies 201 unless told otherwise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list, dict]] = []
        self.urls: list[str] = []
        self.statuses: dict[str, list[int]] = {}
        self.counts: dict[str, int] = {}

    def fail(self, table: str, *statuses: int) -> "FakeRest":
        self.statuses.setdefault(table, []).extend(statuses)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        self.urls.append(str(request.url))
        body = json.loads(request.content) if request.content else []
        self.calls.append((request.method, table, body, dict(request.headers)))
        if request.method == "GET":
            total = self.counts.get(table, 0)
            return httpx.Response(200, headers={"content-range": f"0-0/{total}"}, json=[])
        pending = self.statuses.get(table)
        if pending:
            status = pending.pop(0)
            return httpx.Response(status, json={"message": "boom", "code": "XX000"})
        return httpx.Response(201, json=[])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self, table: str) -> list[dict]:
        return [row for m, t, body, _h in self.calls if t == table and m == "POST" for row in body]

    def posts(self, table: str) -> int:
        return sum(1 for m, t, _b, _h in self.calls if t == table and m == "POST")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retries must not actually wait during a test run."""
    async def instant(_seconds):
        return None

    monkeypatch.setattr("gitscout.supabase.asyncio.sleep", instant)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        tokens=("tok",),
        db_path=str(tmp_path / "t.db"),
        supabase_url="https://proj.supabase.co",
        supabase_key=KEY,
        supabase_batch=2,
    )


@pytest.fixture
def seeded(settings):
    """alice: profile + 2 addresses + an issue she filed and bob commented on."""
    with Store(settings.db_path) as store:
        store.upsert_profile(Profile(login="alice", type="User", name="Alice", followers=10))
        store.upsert_profile(Profile(login="bob", type="User", name="Bob"))
        store.add_interactions(
            [
                Interaction("o/r", "alice", "issues", "User", "2026-01-01T00:00:00Z"),
                Interaction("o/r", "bob", "forks", "User", "2026-01-02T00:00:00Z"),
            ]
        )
        store.add_emails(
            "alice",
            [
                EmailCandidate("alice@corp.io", "gql_profile", 0.95),
                EmailCandidate("a@commit.dev", "gql_commit", 0.8),
            ],
        )
        store.upsert_threads(
            [
                ThreadRecord("o/r", "issues", 7, title="Crash", author="alice",
                             author_type="User", created_at="2026-01-01T00:00:00Z",
                             labels=("bug",), locked=False)
            ]
        )
        store.add_thread_comments(
            [ThreadComment("o/r", "issues", 7, "c1", author="bob", author_type="User",
                           body="me too", created_at="2026-01-02T00:00:00Z")]
        )
        yield store


# ------------------------------------------------------------------- the client


def test_the_client_refuses_to_start_without_a_url_and_key():
    with pytest.raises(SupabaseError, match="not configured"):
        SupabaseClient("", KEY)
    with pytest.raises(SupabaseError, match="not configured"):
        SupabaseClient("https://proj.supabase.co", "")


@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://p.supabase.co", "https://p.supabase.co/rest/v1"),
        ("https://p.supabase.co/", "https://p.supabase.co/rest/v1"),
        ("https://p.supabase.co/rest/v1", "https://p.supabase.co/rest/v1"),
    ],
)
def test_the_rest_path_is_added_once(given, expected):
    assert SupabaseClient(given, KEY).base == expected


def test_upsert_batches_and_asks_postgrest_to_merge(seeded, settings):
    rest = FakeRest()

    async def go():
        async with SupabaseClient(
            settings.supabase_url, KEY, transport=rest.transport
        ) as client:
            return await client.upsert(
                "people",
                [{"login": f"u{i}"} for i in range(5)],
                on_conflict="login",
                batch=2,
            )

    assert run(go()) == 5
    assert rest.posts("people") == 3  # 2 + 2 + 1
    _method, _table, _body, headers = rest.calls[0]
    assert headers["prefer"] == "resolution=merge-duplicates,return=minimal"
    assert "on_conflict=login" in rest.urls[0]
    assert headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried(seeded, settings, status):
    rest = FakeRest().fail("people", status, status)

    async def go():
        async with SupabaseClient(
            settings.supabase_url, KEY, transport=rest.transport
        ) as client:
            return await client.upsert(
                "people", [{"login": "a"}], on_conflict="login", batch=500
            )

    assert run(go()) == 1
    assert rest.posts("people") == 3  # two failures, then success


def test_a_permanent_error_is_not_retried_and_explains_itself(seeded, settings):
    rest = FakeRest().fail("people", 404)

    async def go():
        async with SupabaseClient(
            settings.supabase_url, KEY, transport=rest.transport
        ) as client:
            await client.upsert(
                "people", [{"login": "a"}], on_conflict="login", batch=500
            )

    with pytest.raises(SupabaseError) as caught:
        run(go())
    assert "Run `gitscout supabase schema`" in str(caught.value)
    assert rest.posts("people") == 1


def test_the_service_key_never_appears_in_an_error(seeded, settings):
    rest = FakeRest().fail("people", 403)

    async def go():
        async with SupabaseClient(
            settings.supabase_url, KEY, transport=rest.transport
        ) as client:
            await client.upsert(
                "people", [{"login": "a"}], on_conflict="login", batch=500
            )

    with pytest.raises(SupabaseError) as caught:
        run(go())
    assert KEY not in str(caught.value)


# ------------------------------------------------------- referential safety


def test_collect_pulls_in_a_parent_lead_an_incremental_batch_would_miss(seeded):
    """A new address for a profile that was not refetched must not orphan."""
    since = watermark_of(collect(seeded, None))  # everything so far is synced
    assert collect(seeded, since)["people"] == []

    # now an address arrives without the profile being touched
    seeded.add_emails("alice", [EmailCandidate("new@corp.io", "hunter", 0.6)])
    payload = collect(seeded, since)

    assert [e["email"] for e in payload["person_emails"]] == ["new@corp.io"]
    assert [lead["login"] for lead in payload["people"]] == ["alice"]  # parent added back


def test_collect_drops_an_address_whose_lead_is_suppressed(seeded):
    seeded.suppress(["alice"])
    payload = collect(seeded, None)

    assert "alice" not in {lead["login"] for lead in payload["people"]}
    assert payload["person_emails"] == []


def test_collect_pulls_in_a_parent_thread_for_an_orphan_comment(seeded):
    since = watermark_of(collect(seeded, None))
    seeded.add_thread_comments(
        [ThreadComment("o/r", "issues", 7, "c2", author="bob", body="still broken")]
    )
    payload = collect(seeded, since)

    assert [c["comment_id"] for c in payload["comments"]] == ["c2"]
    assert [(t["repo"], t["number"]) for t in payload["threads"]] == [("o/r", 7)]


def test_only_with_email_keeps_the_addresses_consistent(seeded):
    payload = collect(seeded, None, only_with_email=True)

    assert {lead["login"] for lead in payload["people"]} == {"alice"}
    assert {e["login"] for e in payload["person_emails"]} == {"alice"}


def test_the_watermark_is_the_newest_local_timestamp():
    assert watermark_of(
        {
            "people": [{"github_fetched_at": "2026-01-01T00:00:00"}],
            "person_emails": [{"found_at": "2026-03-01T00:00:00"}],
            "threads": [{"fetched_at": "2026-02-01T00:00:00"}],
            "comments": [],
        }
    ) == "2026-03-01T00:00:00"
    assert watermark_of({"people": []}) is None


# --------------------------------------------------------------------- the push


def test_a_full_push_sends_parents_before_children(seeded, settings):
    rest = FakeRest()
    result = run(push(settings, seeded, full=True, transport=rest.transport))

    order = [table for method, table, _b, _h in rest.calls if method == "POST"]
    assert order.index("people") < order.index("person_emails")
    assert order.index("threads") < order.index("comments")
    assert result.counts == {
        "repositories": 0, "people": 2, "person_emails": 2,
        "threads": 1, "comments": 1, "interactions": 2,
    }
    assert result.mode == "full"


def test_local_only_columns_are_stripped_and_renamed(seeded, settings):
    rest = FakeRest()
    run(push(settings, seeded, full=True, transport=rest.transport))

    lead = rest.bodies("people")[0]
    assert lead["github_fetched_at"]
    thread = rest.bodies("threads")[0]
    assert "added_at" not in thread and "fetched_at" not in thread
    assert thread["github_fetched_at"]
    assert thread["labels"] == ["bug"] and thread["locked"] is False
    comment = rest.bodies("comments")[0]
    assert "changed_at" not in comment and "added_at" not in comment


def test_the_second_push_sends_only_what_changed(seeded, settings):
    first = run(push(settings, seeded, full=True, transport=FakeRest().transport))
    assert first.watermark

    rest = FakeRest()
    second = run(push(settings, seeded, transport=rest.transport))
    assert second.mode == "incremental"
    assert second.since == first.watermark
    assert second.rows == 0
    assert rest.posts("people") == 0

    seeded.upsert_profile(Profile(login="carol", type="User", name="Carol"))
    seeded.add_interactions([Interaction("o/r", "carol", "stars", "User", "2026-02-01T00:00:00Z")])
    third = run(push(settings, seeded, transport=FakeRest().transport))
    assert third.counts["people"] == 1


def test_a_failed_push_does_not_advance_the_watermark(seeded, settings):
    rest = FakeRest().fail("threads", 400)
    before = seeded.get_crawl_state(SYNC_KEY)

    with pytest.raises(SupabaseError):
        run(push(settings, seeded, full=True, transport=rest.transport))

    assert seeded.get_crawl_state(SYNC_KEY) == before
    # the rows that did land are simply upserted again next time
    rest2 = FakeRest()
    assert run(push(settings, seeded, full=True, transport=rest2.transport)).rows > 0


def test_a_dry_run_makes_no_requests(seeded, settings):
    rest = FakeRest()
    result = run(push(settings, seeded, full=True, dry_run=True, transport=rest.transport))

    assert result.dry_run and result.rows == 8
    assert rest.calls == []
    assert seeded.get_crawl_state(SYNC_KEY) is None


def test_the_run_is_logged_remotely(seeded, settings):
    rest = FakeRest()
    run(push(settings, seeded, full=True, transport=rest.transport))

    logged = rest.bodies("sync_runs")
    assert len(logged) == 1
    assert logged[0]["mode"] == "full" and logged[0]["status"] == "ok"
    assert logged[0]["people"] == 2


def test_a_broken_sync_log_does_not_fail_the_push(seeded, settings):
    rest = FakeRest().fail("sync_runs", 400)
    result = run(push(settings, seeded, full=True, transport=rest.transport))

    assert result.rows == 8  # the data landed; only the audit row did not
    assert seeded.get_crawl_state(SYNC_KEY) is not None


# ----------------------------------------------------------------------- the CLI


def test_the_schema_command_prints_runnable_ddl(tmp_path):
    res = runner.invoke(app, ["supabase", "schema"])

    assert res.exit_code == 0
    assert "create table if not exists gitscout.people" in res.output
    assert "enable row level security" in res.output

    out = tmp_path / "schema.sql"
    res = runner.invoke(app, ["supabase", "schema", "-o", str(out)])
    assert res.exit_code == 0 and out.read_text(encoding="utf-8") == schema_sql()


def test_push_without_configuration_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    res = runner.invoke(app, ["--db", str(tmp_path / "t.db"), "supabase", "push"])

    assert res.exit_code == 2
    assert "SUPABASE_URL" in res.output


def test_status_reports_pending_counts_without_a_connection(seeded, settings, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    res = runner.invoke(app, ["--db", settings.db_path, "supabase", "status"])

    assert res.exit_code == 0
    assert "configured:  no" in res.output
    assert "people" in res.output


def test_raw_signals_and_the_registry_are_mirrored(seeded, settings):
    """Forks and commits have no thread, so per-person fork detail needs `interactions`;
    every outreach view joins `repositories` for the stable id."""
    seeded.upsert_repository({
        "repo": "o/r", "repository_id": 42, "node_id": "R_1", "owner": "o", "name": "r",
        "url": "https://github.com/o/r", "description": None, "homepage": None,
        "stars": 10, "forks": 2, "language": "Go", "topics": '["security"]',
        "is_archived": 0,
    })
    rest = FakeRest()
    run(push(settings, seeded, full=True, transport=rest.transport))

    order = [t for m, t, _b, _h in rest.calls if m == "POST"]
    assert order[0] == "repositories"  # first: everything references it
    repo = rest.bodies("repositories")[0]
    assert (repo["repository_id"], repo["topics"], repo["is_archived"]) == (42, ["security"], False)
    assert "github_fetched_at" in repo and "fetched_at" not in repo
    signals = rest.bodies("interactions")
    assert {(s["login"], s["kind"]) for s in signals} == {("alice", "issues"), ("bob", "forks")}
    assert "first_recorded_at" in signals[0] and "added_at" not in signals[0]


# ------------------------------------------------- the direct-Postgres push path

import gitscout.supabase as sup  # noqa: E402
from gitscout.supabase import REFRESH_KEY, _adapt  # noqa: E402


class FakeWriter:
    """Stands in for PostgresWriter: records every upsert and function call."""

    log: list = []
    fail_on: str | None = None   # "upsert" or "refresh"

    def __init__(self, dsn, *, batch=500, connect_timeout=15):
        self.statements = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        FakeWriter.log.append(("commit" if exc_type is None else "rollback",))
        return False

    def upsert(self, table, rows, *, on_conflict):
        if FakeWriter.fail_on == "upsert" and table.endswith(".threads"):
            raise SupabaseError("threads: simulated failure")
        FakeWriter.log.append(("upsert", table, len(rows), on_conflict))
        self.statements += 1
        return len(rows)

    def scalar(self, sql, params=()):
        FakeWriter.log.append(("call", sql, tuple(params)))
        self.statements += 1
        if "refresh_outreach" in sql:
            if FakeWriter.fail_on == "refresh":
                raise SupabaseError("simulated refresh failure")
            return 4231
        if "ensure_repo_schema" in sql:
            return "defendermate_" + params[0].split("/")[1] + "_gitscout"
        return "final_outreach_users_" + params[0].split("/")[1]


@pytest.fixture
def pg(monkeypatch, settings):
    FakeWriter.log = []
    FakeWriter.fail_on = None
    monkeypatch.setattr(sup, "PostgresWriter", FakeWriter)
    return Settings(
        tokens=("tok",), db_path=settings.db_path,
        supabase_db_url="postgresql://gitscout_writer.ref:x@host:6543/postgres",
        supabase_schema="gitscout",
    )


def _register(store):
    store.upsert_repository({
        "repo": "o/r", "repository_id": 42, "node_id": "R_1", "owner": "o", "name": "r",
        "url": "https://github.com/o/r", "description": None, "homepage": None,
        "stars": 10, "forks": 2, "language": "Go", "topics": "[]", "is_archived": 0,
    })


def test_a_full_postgres_push_lands_everything_then_refreshes(seeded, pg):
    _register(seeded)
    result = run(push(pg, seeded, full=True))

    upserts = [e for e in FakeWriter.log if e[0] == "upsert"]
    tables = [e[1] for e in upserts]
    # shared tables in gitscout, repository-scoped ones in the repo's own schema;
    # registry first, parents before children, audit row last
    assert tables[0] == "gitscout.repositories" and tables[-1] == "gitscout.sync_runs"
    assert tables.index("gitscout.people") < tables.index("gitscout.person_emails")
    assert "defendermate_r_gitscout.threads" in tables
    assert tables.index("defendermate_r_gitscout.threads") < tables.index("defendermate_r_gitscout.comments")
    assert "defendermate_r_gitscout.interactions" in tables
    assert not any(t.startswith("gitscout.threads") or t.startswith("gitscout.comments") for t in tables)
    # the repo schema is ensured inside the data transaction, before its rows
    calls = [e[1] for e in FakeWriter.log if e[0] == "call"]
    assert calls[0] == "select gitscout.ensure_repo_schema(%s)"
    first_commit = FakeWriter.log.index(("commit",))
    assert FakeWriter.log.index(("call", "select gitscout.ensure_repo_schema(%s)", ("o/r",))) < first_commit
    # data commits BEFORE any view or refresh call
    assert calls[1:] == ["select gitscout.ensure_outreach_view(%s)", "select gitscout.refresh_outreach()"]
    assert result.refreshed and result.outreach_rows == 4231
    assert result.outreach_views == ["final_outreach_users_r"]
    assert seeded.get_crawl_state(SYNC_KEY)["high_water"] == result.watermark
    assert seeded.get_crawl_state(REFRESH_KEY)["done"] is True


def test_an_unchanged_run_does_not_rebuild_the_dataset(seeded, pg):
    _register(seeded)
    run(push(pg, seeded, full=True))
    FakeWriter.log = []

    result = run(push(pg, seeded))

    assert result.counts["repositories"] == 1 and result.rows == 1  # registry only
    rebuilds = [e for e in FakeWriter.log if e[0] == "call" and "ensure_repo_schema" not in e[1]]
    assert rebuilds == []  # the schema check runs every time; the rebuild must not
    assert not result.refreshed and result.refresh_error is None


def test_a_failed_refresh_keeps_the_data_and_is_retried_next_run(seeded, pg):
    _register(seeded)
    FakeWriter.fail_on = "refresh"
    first = run(push(pg, seeded, full=True))

    # data landed and the watermark moved: the failure was only the rebuild
    assert first.refresh_error and not first.refreshed
    assert seeded.get_crawl_state(SYNC_KEY)["high_water"] == first.watermark
    assert seeded.get_crawl_state(REFRESH_KEY)["done"] is False

    # next run brings no new data, but the pending refresh is retried
    FakeWriter.fail_on = None
    FakeWriter.log = []
    second = run(push(pg, seeded))
    assert second.rows == 1 and second.refreshed
    assert seeded.get_crawl_state(REFRESH_KEY)["done"] is True


def test_a_failed_data_push_moves_nothing(seeded, pg):
    _register(seeded)
    FakeWriter.fail_on = "upsert"
    with pytest.raises(SupabaseError):
        run(push(pg, seeded, full=True))

    assert ("rollback",) in FakeWriter.log
    rebuilds = [e for e in FakeWriter.log if e[0] == "call" and "ensure_repo_schema" not in e[1]]
    assert rebuilds == []  # no refresh of half data
    assert seeded.get_crawl_state(SYNC_KEY) is None


def test_dicts_become_jsonb_and_everything_else_passes_through():
    # psycopg is the optional `postgres` extra; CI installs it, a minimal install may not.
    Jsonb = pytest.importorskip("psycopg.types.json").Jsonb

    assert isinstance(_adapt({"9": 4}), Jsonb)
    assert _adapt(["a", "b"]) == ["a", "b"]
    assert _adapt(None) is None and _adapt(3) == 3


def test_a_schema_name_is_never_interpolated_unchecked(seeded, pg):
    bad = Settings(tokens=("tok",), db_path=pg.db_path, supabase_db_url=pg.supabase_db_url,
                   supabase_schema="gitscout; drop table x")
    with pytest.raises(SupabaseError, match="plain identifier"):
        run(push(bad, seeded, full=True))


def test_the_mirror_defaults_to_its_own_schema_not_public():
    """Regression: the default stayed "public" while every table lived in "gitscout",
    so the CLI push targeted relations that do not exist."""
    from gitscout.config import Settings, load_settings
    from gitscout.supabase import DEFAULT_SCHEMA

    assert Settings().supabase_schema == DEFAULT_SCHEMA == "gitscout"
    assert load_settings().supabase_schema == "gitscout"


def test_rows_for_an_unregistered_repository_are_refused_not_misrouted(seeded, pg):
    """A thread for a repo nobody registered has no schema to go to. Refusing is the
    only safe answer: silently writing it into another repo's schema would mix data."""
    FakeWriter.log = []
    with pytest.raises(SupabaseError, match="not registered"):
        run(push(pg, seeded, full=True))
    assert ("rollback",) in FakeWriter.log
