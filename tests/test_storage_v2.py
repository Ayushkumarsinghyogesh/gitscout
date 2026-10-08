"""The storage added for the scheduled path: crawl state, run log, migrations."""
from __future__ import annotations

import sqlite3

import pytest

from gitscout.models import EmailCandidate, Interaction, Profile, RunRecord
from gitscout.storage import Store


def test_crawl_state_patches_only_what_you_pass():
    with Store(":memory:") as store:
        assert store.get_crawl_state("k") is None
        store.set_crawl_state("k", cursor="c1", done=False, total_count=500)
        assert store.get_crawl_state("k") == {
            "cursor": "c1",
            "done": False,
            "high_water": None,
            "total_count": 500,
        }
        store.set_crawl_state("k", high_water="2024-06-01")
        state = store.get_crawl_state("k")
        assert state["cursor"] == "c1" and state["high_water"] == "2024-06-01"


def test_high_water_only_moves_forward():
    """An interrupted run must never let a later run skip what it did not record."""
    with Store(":memory:") as store:
        store.set_crawl_state("k", high_water="2024-06-01")
        store.set_crawl_state("k", high_water="2024-01-01")  # older: ignored
        assert store.get_crawl_state("k")["high_water"] == "2024-06-01"
        store.set_crawl_state("k", high_water="2024-09-01")
        assert store.get_crawl_state("k")["high_water"] == "2024-09-01"


def test_clear_crawl_state():
    with Store(":memory:") as store:
        store.set_crawl_state("k", cursor="c")
        store.clear_crawl_state("k")
        assert store.get_crawl_state("k") is None


def test_interactions_record_when_they_were_added():
    with Store(":memory:") as store:
        store.add_interactions([Interaction("o/r", "a", "stars")])
        row = store._conn.execute("SELECT added_at FROM interactions").fetchone()
        assert row["added_at"]


def test_add_emails_reports_new_rows_only():
    with Store(":memory:") as store:
        cands = [EmailCandidate("a@x.dev", "gql_profile", 0.95)]
        assert store.add_emails("a", cands) == 1
        assert store.add_emails("a", cands) == 0  # same (login, email, source)
        assert store.add_emails("a", [EmailCandidate("a@x.dev", "gql_commit", 0.85)]) == 1
        assert store.add_emails("a", []) == 0


def test_logins_without_email_filters_and_orders():
    with Store(":memory:") as store:
        for login, followers in (("low", 1), ("high", 900)):
            store.upsert_profile(Profile(login=login, type="User", followers=followers))
        store.upsert_profile(Profile(login="hasmail", type="User"))
        store.upsert_profile(Profile(login="botty", type="Bot"))
        store.upsert_profile(Profile(login="gone", type="Missing", found=False))
        store.add_emails("hasmail", [EmailCandidate("x@y.dev", "gql_profile", 0.95)])

        assert store.logins_without_email() == ["high", "low"]  # most followers first
        assert store.logins_without_email(limit=1) == ["high"]

        store.mark_probed(["high"], "commit_probed_at")
        assert store.logins_without_email() == ["low"]
        assert store.logins_without_email(refresh=True) == ["high", "low"]
        # the provider stage tracks its own column
        assert store.logins_without_email(column="provider_probed_at") == ["high", "low"]


def test_mark_probed_rejects_unknown_columns():
    with Store(":memory:") as store:
        try:
            store.mark_probed(["a"], "drop_table")
        except ValueError as exc:
            assert "unknown probe column" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


def test_profiles_roundtrip_including_graphql_fields():
    with Store(":memory:") as store:
        store.upsert_profile(
            Profile(
                login="alice",
                type="User",
                name="Alice",
                hireable=True,
                node_id="U_123",
                created_at="2015-01-01T00:00:00Z",
            )
        )
        loaded = store.profiles(["alice", "missing"])
        assert set(loaded) == {"alice"}
        assert loaded["alice"].node_id == "U_123"
        assert loaded["alice"].created_at == "2015-01-01T00:00:00Z"
        assert loaded["alice"].hireable is True
        assert store.profiles([]) == {}


def test_upsert_keeps_node_id_when_a_later_write_omits_it():
    with Store(":memory:") as store:
        store.upsert_profile(Profile(login="a", node_id="U_1", created_at="2020-01-01"))
        store.upsert_profile(Profile(login="a", name="A"))  # e.g. the REST path
        loaded = store.profiles(["a"])["a"]
        assert loaded.node_id == "U_1" and loaded.created_at == "2020-01-01"


def test_known_logins():
    with Store(":memory:") as store:
        store.upsert_profile(Profile(login="a"))
        assert store.known_logins(["a", "b"]) == {"a"}
        assert store.known_logins([]) == set()


def test_scores_roundtrip_and_ordering():
    with Store(":memory:") as store:
        store.add_interactions(
            [Interaction("o/r", "a", "stars"), Interaction("o/r", "b", "stars")]
        )
        store.upsert_profile(Profile(login="a", type="User", followers=1000))
        store.upsert_profile(Profile(login="b", type="User", followers=1))
        store.add_emails("a", [EmailCandidate("a@x.dev", "gql_profile", 0.9)])
        store.add_emails("b", [EmailCandidate("b@x.dev", "gql_profile", 0.9)])
        store.set_scores({"a": 10.0, "b": 90.0})

        by_score = [r["login"] for r in store.export_rows(order_by_score=True)]
        by_conf = [r["login"] for r in store.export_rows(order_by_score=False)]
        assert by_score == ["b", "a"] and by_conf == ["a", "b"]
        store.set_scores({})  # no-op


def test_scoring_inputs_aggregates_signals():
    with Store(":memory:") as store:
        store.add_interactions(
            [
                Interaction("o/r", "a", "stars", "User", "2024-01-01"),
                Interaction("o/r2", "a", "prs", "User", "2024-05-01"),
            ]
        )
        store.upsert_profile(Profile(login="a", type="User"))
        store.add_emails("a", [EmailCandidate("a@x.dev", "gql_profile", 0.95)])
        row = store.scoring_inputs()[0]
        assert set(row["kinds"].split(",")) == {"stars", "prs"}
        assert row["repo_count"] == 2
        assert row["last_seen"] == "2024-05-01"
        assert row["email_conf"] == 0.95


def test_run_log_records_outcomes():
    with Store(":memory:") as store:
        run_id = store.start_run("watch", "o/r")
        assert run_id > 0
        store.finish_run(
            RunRecord(
                run_id=run_id,
                interactions_new=5,
                emails_new=2,
                points_used=11,
                status="ok",
                extra={"note": "first"},
            )
        )
        runs = store.recent_runs()
        assert len(runs) == 1
        assert (runs[0]["interactions_new"], runs[0]["status"]) == (5, "ok")
        assert runs[0]["finished_at"]
        assert store.last_successful_run_at() == runs[0]["started_at"]
        assert store.last_successful_run_at("nope") is None


def test_failed_runs_are_not_treated_as_successful():
    with Store(":memory:") as store:
        rid = store.start_run("watch", "o/r")
        store.finish_run(RunRecord(run_id=rid, status="failed", error="boom"))
        assert store.last_successful_run_at() is None
        assert store.recent_runs()[0]["error"] == "boom"


def test_stats_reports_sources_and_hit_rate():
    with Store(":memory:") as store:
        store.add_interactions(
            [Interaction("o/r", "a", "stars"), Interaction("o/r", "b", "stars")]
        )
        store.add_emails("a", [EmailCandidate("a@x.dev", "gql_profile", 0.95)])
        s = store.stats()
        assert s["by_source"] == {"gql_profile": 1}
        assert s["hit_rate"] == 0.5
        assert s["runs"] == 0


def test_hit_rate_is_zero_on_an_empty_database():
    with Store(":memory:") as store:
        assert store.stats()["hit_rate"] == 0.0


def test_new_since_filters_to_this_run(tmp_path):
    db = str(tmp_path / "t.db")
    with Store(db) as store:
        store.add_interactions([Interaction("o/r", "old", "stars", "User", "2024-01-01")])
        cutoff = store._conn.execute("SELECT added_at FROM interactions").fetchone()["added_at"]
        store.upsert_profile(Profile(login="old", type="User"))
    # bump the clock past the first insert by writing a later timestamp directly
    with Store(db) as store:
        store.add_interactions([Interaction("o/r", "new", "stars", "User", "2024-02-01")])
        store.upsert_profile(Profile(login="new", type="User"))
        store._conn.execute("UPDATE interactions SET added_at = '2099-01-01' WHERE login = 'new'")
        store._conn.commit()

        everyone = [r["login"] for r in store.export_rows()]
        fresh = [r["login"] for r in store.export_rows(new_since="2098-01-01")]
        assert set(everyone) == {"old", "new"}
        assert fresh == ["new"]
        assert cutoff  # sanity: the column is populated


def test_migration_adds_columns_to_a_v01_database(tmp_path):
    """A database written by v0.1 must open and keep its rows."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE interactions (repo TEXT NOT NULL, login TEXT NOT NULL, kind TEXT NOT NULL,
            user_type TEXT, occurred_at TEXT, extra TEXT, PRIMARY KEY (repo, login, kind));
        CREATE TABLE users (login TEXT PRIMARY KEY, type TEXT, name TEXT, company TEXT, bio TEXT,
            location TEXT, blog TEXT, twitter TEXT, public_email TEXT, hireable INTEGER,
            followers INTEGER, profile_found INTEGER NOT NULL DEFAULT 1, fetched_at TEXT,
            discovered_at TEXT);
        CREATE TABLE emails (login TEXT NOT NULL, email TEXT NOT NULL, source TEXT NOT NULL,
            confidence REAL NOT NULL, found_at TEXT NOT NULL, PRIMARY KEY (login, email, source));
        CREATE TABLE checkpoints (key TEXT PRIMARY KEY, next_url TEXT,
            done INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
        CREATE TABLE suppression (value TEXT PRIMARY KEY, added_at TEXT NOT NULL);
        INSERT INTO interactions (repo, login, kind) VALUES ('o/r', 'alice', 'stars');
        INSERT INTO users (login, type) VALUES ('alice', 'User');
        """
    )
    conn.commit()
    conn.close()

    with Store(db) as store:
        assert store.stats()["interactions"] == 1
        # the new columns and tables now exist and work
        store.set_crawl_state("k", cursor="c")
        store.set_scores({"alice": 50.0})
        rid = store.start_run("scout", "o/r")
        store.finish_run(RunRecord(run_id=rid, status="ok"))
        assert store.export_rows()[0]["score"] == 50.0
        assert store.add_interactions([Interaction("o/r", "bob", "stars")]) == 1

    # and opening it a second time is a no-op, not a duplicate-column error
    with Store(db) as store:
        assert store.stats()["interactions"] == 2


def test_add_emails_deduplicates_within_one_call():
    with Store(":memory:") as store:
        dupes = [
            EmailCandidate("a@x.dev", "gql_commit", 0.85),
            EmailCandidate("a@x.dev", "gql_commit", 0.85),
        ]
        assert store.add_emails("a", dupes) == 1


def test_add_emails_updates_confidence_without_recounting():
    with Store(":memory:") as store:
        store.add_emails("a", [EmailCandidate("a@x.dev", "gql_commit", 0.4)])
        assert store.add_emails("a", [EmailCandidate("a@x.dev", "gql_commit", 0.85)]) == 0
        row = store._conn.execute("SELECT confidence FROM emails").fetchone()
        assert row["confidence"] == 0.85  # the value is still refreshed


def test_organizations_and_bots_are_never_exported_as_leads():
    """A company forking your repo is a signal, not a person to email."""
    with Store(":memory:") as store:
        store.add_interactions(
            [
                Interaction("o/r", "alice", "stars", "User", "2024-01-01"),
                Interaction("o/r", "acme", "forks", "Organization", "2024-01-01"),
                Interaction("o/r", "ci-bot", "stars", "Bot", "2024-01-01"),
                Interaction("o/r", "ghosty", "issues", "Mannequin", "2024-01-01"),
            ]
        )
        store.upsert_profile(Profile(login="alice", type="User"))
        assert [r["login"] for r in store.export_rows()] == ["alice"]


def test_a_stored_organization_profile_is_also_excluded():
    with Store(":memory:") as store:
        store.add_interactions([Interaction("o/r", "acme", "forks", None, "2024-01-01")])
        store.upsert_profile(Profile(login="acme", type="Organization"))
        assert store.export_rows() == []


# ------------------------------------------------------------- inspection API


def _inspectable(tmp_path):
    store = Store(str(tmp_path / "i.db"))
    store.add_interactions([Interaction("o/r", "alice", "stars", "User", "2024-01-01")])
    store.upsert_profile(Profile(login="alice", type="User", name="Alice"))
    store.add_emails(
        "alice",
        [
            EmailCandidate("a@acme.dev", "gql_profile", 0.95),
            EmailCandidate("alice@old.dev", "gql_commit", 0.85),
        ],
    )
    return store


def test_table_names_and_counts(tmp_path):
    with _inspectable(tmp_path) as store:
        names = store.table_names()
        assert {"interactions", "users", "emails", "crawl_state", "runs"} <= set(names)
        assert "sqlite_sequence" not in names  # internal tables are hidden
        counts = store.table_counts()
        assert counts["users"] == 1 and counts["emails"] == 2


def test_columns_and_unknown_table(tmp_path):
    with _inspectable(tmp_path) as store:
        assert "login" in store.columns("users")
        with pytest.raises(ValueError, match="no such table"):
            store.columns("nope")


def test_table_rows_honours_limit_and_ordering(tmp_path):
    with _inspectable(tmp_path) as store:
        rows = store.table_rows("emails", limit=1)
        assert len(rows) == 1
        ordered = store.table_rows("emails", order_by="confidence")
        assert [r["confidence"] for r in ordered] == [0.95, 0.85]
        with pytest.raises(ValueError, match="no such table"):
            store.table_rows("users; DROP TABLE users")
        with pytest.raises(ValueError, match="no column"):
            store.table_rows("emails", order_by="bogus")


def test_read_query_returns_dicts(tmp_path):
    with _inspectable(tmp_path) as store:
        rows = store.read_query("SELECT login, COUNT(*) AS n FROM emails GROUP BY login")
        assert rows == [{"login": "alice", "n": 2}]
        assert store.read_query("WITH x AS (SELECT 1 AS v) SELECT v FROM x") == [{"v": 1}]


def test_read_query_refuses_writes(tmp_path):
    """Two layers: the prefix check, and a connection SQLite refuses to write through."""
    with _inspectable(tmp_path) as store:
        for bad in (
            "DELETE FROM users",
            "DROP TABLE users",
            "UPDATE users SET score = 0",
            "INSERT INTO users (login) VALUES ('x')",
        ):
            with pytest.raises(ValueError, match="only SELECT"):
                store.read_query(bad)

        # valid SQLite that starts with WITH but writes: the prefix check passes, so the
        # read-only connection is what has to stop it
        with pytest.raises(ValueError, match="readonly|query failed"):
            store.read_query("WITH x AS (SELECT 1) DELETE FROM users")

        # and the data is untouched
        assert store.table_counts()["users"] == 1


def test_read_query_refuses_multiple_statements(tmp_path):
    with _inspectable(tmp_path) as store:
        with pytest.raises(ValueError, match="one statement"):
            store.read_query("SELECT 1; DROP TABLE users")
        with pytest.raises(ValueError, match="empty query"):
            store.read_query("   ")


def test_read_query_reports_bad_sql_clearly(tmp_path):
    with _inspectable(tmp_path) as store:
        with pytest.raises(ValueError, match="query failed"):
            store.read_query("SELECT nope FROM users")


def test_read_query_works_on_an_in_memory_database():
    with Store(":memory:") as store:
        assert store.read_query("SELECT 1 AS v") == [{"v": 1}]


def test_emails_for_shows_every_address_best_first(tmp_path):
    """The CSV only carries the best email; this is how you see the rest."""
    with _inspectable(tmp_path) as store:
        found = store.emails_for("alice")
        assert [e["email"] for e in found] == ["a@acme.dev", "alice@old.dev"]
        assert store.emails_for("nobody") == []


def test_discovery_done_counts_people_found_during_ingest():
    """A profile email arrives at ingest, so those users are never commit-probed."""
    with Store(":memory:") as store:
        store.add_interactions(
            [
                Interaction("o/r", "found-at-ingest", "stars", "User", "2024-01-01"),
                Interaction("o/r", "probed-no-hit", "stars", "User", "2024-01-01"),
                Interaction("o/r", "not-yet", "stars", "User", "2024-01-01"),
            ]
        )
        for login in ("found-at-ingest", "probed-no-hit", "not-yet"):
            store.upsert_profile(Profile(login=login, type="User"))
        store.add_emails("found-at-ingest", [EmailCandidate("a@x.dev", "gql_profile", 0.95)])
        store.mark_probed(["probed-no-hit"], "discovered_at")

        s = store.stats()
        assert s["discovery_done"] == 2  # has an email, or was probed
        assert s["discovery_pending"] == 1  # only `not-yet` is outstanding


def test_discovery_done_counts_users_whose_email_arrived_during_ingest():
    """Those users are never commit-probed, so `discovered_at` alone understated it."""
    with Store(":memory:") as store:
        store.add_interactions(
            [
                Interaction("o/r", "found", "contribs", "User", "2024-01-01"),
                Interaction("o/r", "probed", "issues", "User", "2024-01-01"),
                Interaction("o/r", "waiting", "stars", "User", "2024-01-01"),
            ]
        )
        for login in ("found", "probed", "waiting"):
            store.upsert_profile(Profile(login=login, type="User"))
        # email came straight from ingest: no probe, no discovered_at
        store.add_emails("found", [EmailCandidate("f@x.dev", "gql_contrib", 0.85)])
        # probed and came back empty
        store.mark_probed(["probed"], "commit_probed_at")
        store.mark_probed(["probed"], "discovered_at")

        s = store.stats()
        assert s["discovery_done"] == 2  # found + probed
        assert s["discovery_pending"] == 1  # waiting
