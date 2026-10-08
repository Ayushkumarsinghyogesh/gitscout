import csv

from typer.testing import CliRunner

from gitscout.cli import app
from gitscout.models import EmailCandidate, Interaction, Profile
from gitscout.storage import Store

runner = CliRunner()


def _seed(db: str) -> None:
    with Store(db) as store:
        store.add_interactions([Interaction("o/r", "alice", "stars", "User", "2024-01-01")])
        store.upsert_profile(Profile(login="alice", type="User", name="Alice"))
        store.add_emails("alice", [EmailCandidate("alice@a.dev", "profile", 0.95)])


def test_help_lists_commands():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("ingest", "enrich", "export", "run", "stats", "suppress", "rate-limit"):
        assert cmd in res.output


def test_stats_export_and_suppress(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)

    res = runner.invoke(app, ["--db", db, "stats"])
    assert res.exit_code == 0 and "users with email:  1" in res.output

    out = tmp_path / "out.csv"
    res = runner.invoke(app, ["--db", db, "export", str(out), "--only-with-email"])
    assert res.exit_code == 0 and "Wrote 1 rows" in res.output
    assert [r["email"] for r in csv.DictReader(out.open(encoding="utf-8"))] == ["alice@a.dev"]

    res = runner.invoke(app, ["--db", db, "suppress", "alice"])
    assert res.exit_code == 0 and "Added 1" in res.output
    res = runner.invoke(app, ["--db", db, "export", str(out), "--only-with-email"])
    assert "Wrote 0 rows" in res.output


def test_invalid_repo_and_kinds_fail_cleanly(tmp_path):
    db = str(tmp_path / "t.db")
    res = runner.invoke(app, ["--db", db, "ingest", "not a repo"])
    assert res.exit_code == 2

    res = runner.invoke(app, ["--db", db, "ingest", "o/r", "--kinds", "likes"])
    assert res.exit_code != 0
