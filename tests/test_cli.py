import csv

from typer.testing import CliRunner

from gitscout.cli import app
from gitscout.models import (
    EmailCandidate,
    Interaction,
    Profile,
    ThreadComment,
    ThreadRecord,
)
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
    for cmd in (
        "ingest", "enrich", "export", "run", "stats", "suppress", "rate-limit",
        "threads", "thread-tails",
    ):
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


def test_threads_export_only_needs_no_token(tmp_path):
    """`--export-only` must never touch the network, so it works with no token set."""
    db = str(tmp_path / "t.db")
    _seed(db)
    with Store(db) as store:
        store.upsert_threads(
            [
                ThreadRecord("o/r", "issues", 4, title="Crash", body="trace", author="alice",
                             author_type="User", created_at="2026-01-01T00:00:00Z",
                             comment_count=1, comments_stored=1),
                ThreadRecord("o/r", "prs", 5, title="Fix it", author="alice",
                             author_type="User", created_at="2026-01-02T00:00:00Z"),
            ]
        )
        store.add_thread_comments(
            [ThreadComment("o/r", "issues", 4, "c1", author="alice", author_type="User",
                           body="ping", created_at="2026-01-02T00:00:00Z")]
        )

    out = tmp_path / "threads.csv"
    comments = tmp_path / "comments.csv"
    res = runner.invoke(
        app,
        ["--db", db, "threads", "o/r", "--export-only", "-o", str(out),
         "--comments-out", str(comments)],
    )

    assert res.exit_code == 0, res.output
    assert "2 threads" in res.output
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [(r["login"], r["section"], r["number"]) for r in rows] == [
        ("alice", "prs", "5"),
        ("alice", "issues", "4"),
    ]
    assert len(list(csv.DictReader(comments.open(encoding="utf-8")))) == 1


def test_threads_can_export_a_single_section(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    with Store(db) as store:
        store.upsert_threads(
            [
                ThreadRecord("o/r", "issues", 1, author="alice", author_type="User"),
                ThreadRecord("o/r", "prs", 2, author="alice", author_type="User"),
            ]
        )
    out = tmp_path / "prs.csv"
    res = runner.invoke(
        app, ["--db", db, "threads", "o/r", "--export-only", "--sections", "prs", "-o", str(out)]
    )

    assert res.exit_code == 0, res.output
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["section"] for r in rows] == ["prs"]


def test_bad_role_and_section_fail_cleanly(tmp_path):
    db = str(tmp_path / "t.db")
    res = runner.invoke(app, ["--db", db, "threads", "--role", "lurker"])
    assert res.exit_code == 2 and "--role must be one of" in res.output

    res = runner.invoke(app, ["--db", db, "threads", "o/r", "--export-only", "--sections", "stars"])
    assert res.exit_code != 0


def test_stats_reports_the_thread_layer_once_it_exists(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    res = runner.invoke(app, ["--db", db, "stats"])
    assert "threads stored" not in res.output  # nothing to say before the stage has run

    with Store(db) as store:
        store.upsert_threads(
            [ThreadRecord("o/r", "issues", 1, author="alice", author_type="User",
                          comment_count=9, comments_stored=2, comments_truncated=True)]
        )
    res = runner.invoke(app, ["--db", db, "stats"])
    assert "threads stored:    1" in res.output
    assert "threads capped:  1" in res.output
