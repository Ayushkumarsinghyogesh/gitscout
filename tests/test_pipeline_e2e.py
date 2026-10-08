import asyncio
import csv

from helpers import fork, issue, profile, star

from gitscout.config import Settings
from gitscout.pipeline import parse_kinds, run_pipeline

import pytest


def test_parse_kinds():
    assert parse_kinds("stars, forks") == ("stars", "forks")
    assert parse_kinds("stars,stars") == ("stars",)
    with pytest.raises(ValueError):
        parse_kinds("stars,likes")
    with pytest.raises(ValueError):
        parse_kinds("")


def test_end_to_end_pipeline(fake, tmp_path):
    fake.add_pages("/repos/o/r/stargazers", [[star("alice"), star("bob")], [star("bot1", type_="Bot")]])
    fake.add_pages("/repos/o/r/forks", [[fork("carol")]])
    fake.add_pages("/repos/o/r/issues", [[issue("dave", 1), issue("alice", 2, pr=True), issue("alice", 3)]])

    fake.add(
        "/users/alice",
        profile("alice", name="Alice", email="alice@acme.dev", followers=50, bio="=HYPERLINK(\"http://x\")"),
    )
    fake.add("/users/bob", profile("bob", name="Bob B"))
    fake.add(
        "/users/bob/events/public",
        [{"type": "PushEvent", "payload": {"commits": [{"author": {"email": "bob@bob.dev", "name": "Bob B"}}]}}],
    )
    fake.add("/users/carol", profile("carol", email="not-an-email"))
    fake.add("/users/dave", profile("dave"))

    out = tmp_path / "leads.csv"
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))

    result = asyncio.run(
        run_pipeline(settings, ["https://github.com/o/r"], out=out, transport=fake.transport)
    )

    assert sum(r.new for r in result.ingest) == 6  # 3 stars, 1 fork, 2 issues
    assert result.enrich.processed == 4  # alice, bob, carol, dave (bot skipped)
    assert result.enrich.with_email == 2
    assert result.exported == 4

    rows = {r["login"]: r for r in csv.DictReader(out.open(encoding="utf-8"))}
    assert set(rows) == {"alice", "bob", "carol", "dave"}
    assert rows["alice"]["email"] == "alice@acme.dev" and rows["alice"]["email_source"] == "profile"
    assert rows["bob"]["email"] == "bob@bob.dev" and rows["bob"]["email_source"] == "events"
    assert rows["carol"]["email"] == "" and rows["dave"]["email"] == ""
    assert set(rows["alice"]["signals"].split(",")) == {"stars:o/r", "issues:o/r"}
    assert rows["carol"]["signals"] == "forks:o/r"
    assert rows["alice"]["profile_url"] == "https://github.com/alice"
    assert rows["alice"]["bio"].startswith("'=")  # CSV formula injection neutralised

    # second run is a no-op for ingest (checkpoints) and enrich (already discovered)
    calls_before = len(fake.calls)
    again = asyncio.run(run_pipeline(settings, ["o/r"], transport=fake.transport))
    assert all(r.skipped for r in again.ingest)
    assert again.enrich.processed == 0
    assert len(fake.calls) == calls_before


def test_only_with_email_export(fake, tmp_path):
    fake.add_pages("/repos/o/r/stargazers", [[star("alice"), star("dave")]])
    fake.add("/users/alice", profile("alice", email="alice@acme.dev"))
    fake.add("/users/dave", profile("dave"))
    out = tmp_path / "leads.csv"
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    result = asyncio.run(
        run_pipeline(
            settings, ["o/r"], ["stars"], out=out, only_with_email=True, transport=fake.transport
        )
    )
    assert result.exported == 1
    assert [r["login"] for r in csv.DictReader(out.open(encoding="utf-8"))] == ["alice"]
