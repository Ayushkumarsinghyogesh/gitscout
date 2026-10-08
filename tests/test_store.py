from gitscout.models import EmailCandidate, Interaction, Profile
from gitscout.storage import Store


def _seed(store: Store) -> None:
    store.add_interactions(
        [
            Interaction("o/r", "alice", "stars", "User", "2024-01-01"),
            Interaction("o/r", "alice", "issues", "User", "2024-02-01"),
            Interaction("o/r", "bob", "forks", "User", "2024-03-01"),
            Interaction("o/r", "dependabot[bot]", "issues", "Bot", "2024-03-01"),
            Interaction("o/other", "carol", "stars", "User", "2024-04-01"),
        ]
    )
    for login, followers in (("alice", 10), ("bob", 99), ("carol", 1)):
        store.upsert_profile(Profile(login=login, type="User", name=login.title(), followers=followers))
    store.upsert_profile(Profile(login="dependabot[bot]", type="Bot"))
    store.add_emails("alice", [EmailCandidate("alice@a.dev", "profile", 0.95)])
    store.add_emails("bob", [EmailCandidate("bob@b.dev", "events", 0.4)])


def test_interactions_are_idempotent():
    with Store(":memory:") as store:
        rows = [Interaction("o/r", "a", "stars"), Interaction("o/r", "b", "stars")]
        assert store.add_interactions(rows) == 2
        assert store.add_interactions(rows) == 0
        assert store.stats()["interactions"] == 2


def test_checkpoint_roundtrip():
    with Store(":memory:") as store:
        assert store.get_checkpoint("k") is None
        store.set_checkpoint("k", "https://next", done=False)
        assert store.get_checkpoint("k") == ("https://next", False)
        store.set_checkpoint("k", None, done=True)
        assert store.get_checkpoint("k") == (None, True)
        store.clear_checkpoint("k")
        assert store.get_checkpoint("k") is None


def test_logins_to_enrich_skips_bots_and_done():
    with Store(":memory:") as store:
        _seed(store)
        assert store.logins_to_enrich() == ["alice", "bob", "carol"]
        store.mark_discovered("alice")
        assert store.logins_to_enrich() == ["bob", "carol"]
        assert store.logins_to_enrich(refresh=True) == ["alice", "bob", "carol"]
        assert store.logins_to_enrich(limit=1) == ["bob"]


def test_export_aggregates_signals_and_excludes_bots():
    with Store(":memory:") as store:
        _seed(store)
        rows = store.export_rows()
        assert [r["login"] for r in rows] == ["alice", "bob", "carol"]  # emails first, best first
        alice = rows[0]
        assert alice["email"] == "alice@a.dev"
        assert set(alice["signals"].split(",")) == {"stars:o/r", "issues:o/r"}
        assert rows[2]["email"] is None  # carol: no email


def test_export_filters():
    with Store(":memory:") as store:
        _seed(store)
        assert [r["login"] for r in store.export_rows(only_with_email=True)] == ["alice", "bob"]
        assert [r["login"] for r in store.export_rows(only_with_email=True, min_confidence=0.5)] == ["alice"]
        assert [r["login"] for r in store.export_rows(repos=["o/other"])] == ["carol"]


def test_suppression_by_login_and_email():
    with Store(":memory:") as store:
        _seed(store)
        assert store.suppress(["Bob", "alice@a.dev", "  "]) == 2
        rows = store.export_rows()
        assert [r["login"] for r in rows] == ["alice", "carol"]
        assert rows[0]["email"] is None  # alice's email is suppressed, row kept without it
        assert store.export_rows(only_with_email=True) == []


def test_best_email_is_highest_confidence():
    with Store(":memory:") as store:
        _seed(store)
        store.add_emails("bob", [EmailCandidate("bob@work.dev", "commit_api", 0.85)])
        bob = next(r for r in store.export_rows() if r["login"] == "bob")
        assert bob["email"] == "bob@work.dev"
