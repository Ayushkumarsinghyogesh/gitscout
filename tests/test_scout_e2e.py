"""End-to-end: the GraphQL pipeline and the CLI commands that drive it."""
from __future__ import annotations

import asyncio
import csv
import json

import httpx
import pytest
from gqlhelpers import (
    DEFAULT_RATE_LIMIT,
    authored_body,
    commit,
    commit_probe,
    forks_body,
    gql_bot,
    gql_org,
    gql_user,
    op_name,
    stars_body,
)
from typer.testing import CliRunner

from gitscout.cli import app
from gitscout.config import Settings
from gitscout.pipeline import run_scout, targets_for
from gitscout.targets import targets_from_repos

runner = CliRunner()


def run(coro):
    return asyncio.run(coro)


def test_full_graphql_pipeline(gql, tmp_path):
    """Stars + forks + issues + PRs -> profiles -> commit emails -> scored export."""
    gql.add(
        "Stars",
        stars_body(
            [
                (gql_user("alice", name="Alice", email="alice@acme.dev", company="Acme", followers=500), "2024-06-01T00:00:00Z"),
                (gql_user("bob", name="Bob Brown"), "2024-05-01T00:00:00Z"),
                (gql_bot("ci-bot"), "2024-04-01T00:00:00Z"),
            ],
            total=3,
        ),
    )
    gql.add("Forks", forks_body([(gql_user("carol"), "2024-03-01T00:00:00Z"), (gql_org("acme"), "2024-02-01T00:00:00Z")]))
    gql.add("Issues", authored_body("issues", [(gql_user("dave", bio="cloud security engineer"), "2024-07-01T00:00:00Z")]))
    gql.add("PullRequests", authored_body("pullRequests", [(gql_user("erin"), "2024-08-01T00:00:00Z")], extra={"merged": True}))
    # bob/carol/dave/erin had no profile email; the batched probe finds two of them
    gql.add(
        "CommitEmails",
        {
            "u0": commit_probe("bob", name="Bob Brown", repos=[[commit("bob@bob.dev", name="Bob Brown")]]),
            "u1": commit_probe("carol", repos=[[commit("carol@c.dev", linked="carol")]]),
            "u2": commit_probe("dave", repos=[[]]),
            "u3": commit_probe("erin", repos=[[commit("someoneelse@x.dev", linked="stranger")]]),
        },
    )

    out_csv, out_jsonl = tmp_path / "leads.csv", tmp_path / "leads.jsonl"
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    targets = targets_from_repos(["https://github.com/o/r"], ("stars", "forks", "issues", "prs"))

    result = run(
        run_scout(settings, targets, outputs=[out_csv, out_jsonl], transport=gql.transport)
    )

    # 3 stars + 2 forks + 1 issue + 1 PR
    assert result.ingest.new == 7
    assert result.ingest.emails_new == 1  # alice published hers
    assert result.enrich.emails_new == 2  # bob (name match) and carol (linked)
    assert result.emails_new == 3
    assert result.enrich.queries == 1  # four users, one query
    assert result.points == 5  # 4 ingest queries + 1 enrich query
    assert result.scored == 5  # the bot and the org are not scored

    rows = {r["login"]: r for r in csv.DictReader(out_csv.open(encoding="utf-8"))}
    assert set(rows) == {"alice", "bob", "carol", "dave", "erin"}  # no bot, no org
    assert rows["alice"]["email"] == "alice@acme.dev" and rows["alice"]["email_source"] == "gql_profile"
    assert rows["bob"]["email"] == "bob@bob.dev" and rows["bob"]["email_source"] == "gql_commit"
    assert float(rows["bob"]["email_confidence"]) < float(rows["carol"]["email_confidence"])
    assert rows["dave"]["email"] == "" and rows["erin"]["email"] == ""

    # dave filed an issue and reads as ICP, so he outranks a plain stargazer
    assert float(rows["dave"]["score"]) > float(rows["bob"]["score"])

    jsonl = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(jsonl) == 5

    # and the run is in the audit log
    from gitscout.storage import Store

    with Store(settings.db_path) as store:
        log = store.recent_runs()
        assert log[0]["status"] == "ok" and log[0]["interactions_new"] == 7


def test_second_incremental_run_is_cheap_and_finds_nothing(gql, tmp_path):
    page = stars_body([(gql_user("alice", email="a@acme.dev"), "2024-06-01T00:00:00Z")])
    gql.add("Stars", page)
    gql.add("CommitEmails", {})
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    targets = targets_from_repos(["o/r"], ("stars",))

    first = run(run_scout(settings, targets, transport=gql.transport))
    assert first.ingest.new == 1

    second = run(run_scout(settings, targets, incremental=True, transport=gql.transport))
    assert second.ingest.new == 0
    assert second.points == 1  # one page-1 query, then it stopped
    assert all(r.stopped_early for r in second.ingest.results)


def test_new_only_export_contains_just_this_run(gql, tmp_path):
    gql.add("Stars", stars_body([(gql_user("old", email="old@x.dev"), "2024-01-01T00:00:00Z")]))
    gql.add("CommitEmails", {})
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    targets = targets_from_repos(["o/r"], ("stars",))
    run(run_scout(settings, targets, transport=gql.transport))

    gql.queue.clear()
    gql.add("Stars", stars_body([(gql_user("fresh", email="fresh@x.dev"), "2024-09-01T00:00:00Z")]))
    gql.add("CommitEmails", {})
    out = tmp_path / "new.csv"
    result = run(
        run_scout(
            settings, targets, incremental=True, outputs=[out], new_only=True, transport=gql.transport
        )
    )
    assert result.exported == 1
    assert [r["login"] for r in csv.DictReader(out.open(encoding="utf-8"))] == ["fresh"]


def test_a_failed_run_is_recorded_then_raised(gql, tmp_path):
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    targets = targets_from_repos(["o/r"], ("stars",))
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"message": "bad"}))

    with pytest.raises(Exception):
        run(run_scout(settings, targets, transport=transport))

    from gitscout.storage import Store

    with Store(settings.db_path) as store:
        row = store.recent_runs()[0]
        assert row["status"] == "failed" and "bad" in (row["error"] or "")


def test_no_enrich_skips_the_commit_probe(gql, tmp_path):
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")]))
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    result = run(
        run_scout(
            settings,
            targets_from_repos(["o/r"], ("stars",)),
            enrich=False,
            transport=gql.transport,
        )
    )
    assert result.enrich.queries == 0
    assert "CommitEmails" not in gql.ops()


def test_targets_for_merges_profiles_and_cli_repos_without_duplicates():
    targets = targets_for(["o/extra"], ("stars",), "cloud-security")
    repos = [t.repo for t in targets]
    assert "o/extra" in repos and "prowler-cloud/prowler" in repos
    assert len(repos) == len(set(repos))

    with pytest.raises(ValueError, match="nothing to scout"):
        targets_for([], ("stars",), None)


def test_rest_backend_rejects_pr_kind():
    from gitscout.pipeline import ingest_repos

    async def go():
        return await ingest_repos(None, None, ["o/r"], ["prs"])

    with pytest.raises(ValueError, match="does not support"):
        run(go())


# ---------------------------------------------------------------------- CLI


def test_cli_help_lists_old_and_new_commands():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in (
        "scout", "watch", "doctor", "discover", "ingest", "enrich",
        "apify", "export", "run", "score", "stats", "runs", "suppress", "rate-limit",
    ):
        assert cmd in res.output


def test_cli_refuses_to_run_without_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    res = runner.invoke(app, ["--db", str(tmp_path / "t.db"), "scout", "o/r"])
    assert res.exit_code == 2
    assert "401" in res.output and "settings/tokens" in res.output


def test_cli_targets_list_shows_the_shipped_profile():
    res = runner.invoke(app, ["targets-list"])
    assert res.exit_code == 0 and "cloud-security" in res.output

    res = runner.invoke(app, ["targets-list", "--targets", "cloud-security"])
    assert res.exit_code == 0 and "prowler-cloud/prowler" in res.output


def test_cli_doctor_offline_exits_nonzero_without_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "")
    res = runner.invoke(app, ["--db", str(tmp_path / "t.db"), "doctor", "--offline"])
    assert res.exit_code == 1
    assert "[FAIL] github token" in res.output


def test_cli_export_jsonl_and_stats_and_runs(tmp_path):
    from gitscout.models import EmailCandidate, Interaction, Profile
    from gitscout.storage import Store

    db = str(tmp_path / "t.db")
    with Store(db) as store:
        store.add_interactions([Interaction("o/r", "alice", "prs", "User", "2024-01-01")])
        store.upsert_profile(Profile(login="alice", type="User", name="Alice"))
        store.add_emails("alice", [EmailCandidate("alice@a.dev", "gql_profile", 0.95)])
        rid = store.start_run("scout", "o/r")
        from gitscout.models import RunRecord

        store.finish_run(RunRecord(run_id=rid, status="ok", interactions_new=1))

    res = runner.invoke(app, ["--db", db, "score"])
    assert res.exit_code == 0 and "Scored 1" in res.output

    out = tmp_path / "leads.jsonl"
    res = runner.invoke(app, ["--db", db, "export", str(out), "--only-with-email"])
    assert res.exit_code == 0 and "(jsonl)" in res.output
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["email"] == "alice@a.dev"

    res = runner.invoke(app, ["--db", db, "stats"])
    assert res.exit_code == 0 and "email hit rate:    100.0%" in res.output

    res = runner.invoke(app, ["--db", db, "runs"])
    assert res.exit_code == 0 and "scout" in res.output

    res = runner.invoke(app, ["--db", db, "runs", "--json"])
    assert res.exit_code == 0 and json.loads(res.output)[0]["status"] == "ok"


def test_cli_rejects_bad_kinds_and_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "tok")
    db = str(tmp_path / "t.db")
    assert runner.invoke(app, ["--db", db, "scout", "o/r", "--kinds", "likes"]).exit_code != 0
    assert runner.invoke(app, ["--db", db, "scout", "not a repo"]).exit_code != 0
    assert runner.invoke(app, ["--db", db, "export", "x.csv", "--format", "parquet"]).exit_code != 0
    assert runner.invoke(app, ["--db", db, "scout", "o/r", "--targets", "nope"]).exit_code != 0


def test_cli_apify_without_a_token_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "tok")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    res = runner.invoke(app, ["--db", str(tmp_path / "t.db"), "apify", "o/r"])
    assert res.exit_code == 2 and "APIFY_TOKEN" in res.output


def test_cli_db_command(tmp_path):
    from gitscout.models import EmailCandidate, Interaction, Profile
    from gitscout.storage import Store

    db = str(tmp_path / "t.db")
    with Store(db) as store:
        store.add_interactions([Interaction("o/r", "alice", "contribs", "User", "2024-01-01")])
        store.upsert_profile(Profile(login="alice", type="User", name="Alice"))
        store.add_emails(
            "alice",
            [
                EmailCandidate("a@acme.dev", "gql_contrib", 0.85),
                EmailCandidate("a@old.dev", "gql_commit", 0.42),
            ],
        )

    # no options: table listing
    res = runner.invoke(app, ["--db", db, "db"])
    assert res.exit_code == 0
    assert "interactions" in res.output and "emails" in res.output

    # dump a table
    res = runner.invoke(app, ["--db", db, "db", "--table", "users"])
    assert res.exit_code == 0 and "alice" in res.output

    # every email for one person (the CSV shows only the best)
    res = runner.invoke(app, ["--db", db, "db", "--login", "alice"])
    assert res.exit_code == 0
    assert "a@acme.dev" in res.output and "a@old.dev" in res.output

    # custom read-only SQL, and as JSON
    res = runner.invoke(app, ["--db", db, "db", "--sql", "SELECT COUNT(*) AS n FROM emails", "--json"])
    assert res.exit_code == 0 and json.loads(res.output) == [{"n": 2}]

    # writes are refused and change nothing
    for bad in ("DELETE FROM users", "DROP TABLE emails", "WITH x AS (SELECT 1) DELETE FROM users"):
        assert runner.invoke(app, ["--db", db, "db", "--sql", bad]).exit_code != 0
    with Store(db) as store:
        assert store.table_counts()["users"] == 1 and store.table_counts()["emails"] == 2

    # unknown table fails cleanly
    assert runner.invoke(app, ["--db", db, "db", "--table", "nope"]).exit_code != 0


def test_cli_db_exports_rows_to_a_file(tmp_path):
    from gitscout.models import Interaction, Profile
    from gitscout.storage import Store

    db = str(tmp_path / "t.db")
    with Store(db) as store:
        store.add_interactions([Interaction("o/r", "alice", "stars", "User", "2024-01-01")])
        store.upsert_profile(Profile(login="alice", type="User"))

    out = tmp_path / "raw.csv"
    res = runner.invoke(app, ["--db", db, "db", "--table", "interactions", "-o", str(out)])
    assert res.exit_code == 0 and "Wrote 1 rows" in res.output
    assert "alice" in out.read_text(encoding="utf-8")

    out_jsonl = tmp_path / "raw.jsonl"
    res = runner.invoke(app, ["--db", db, "db", "--table", "users", "-o", str(out_jsonl)])
    assert res.exit_code == 0
    assert json.loads(out_jsonl.read_text(encoding="utf-8").splitlines()[0])["login"] == "alice"


def test_cli_db_empty_table_is_not_an_error(tmp_path):
    res = runner.invoke(app, ["--db", str(tmp_path / "t.db"), "db", "--table", "suppression"])
    assert res.exit_code == 0 and "no rows" in res.output
