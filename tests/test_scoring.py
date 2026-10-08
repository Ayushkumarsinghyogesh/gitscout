"""Lead scoring: the weighted sum, and that it stays inside 0-100."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gitscout.models import ALL_KINDS, EmailCandidate, Interaction, Profile
from gitscout.scoring import KIND_WEIGHT, explain, rescore, score_row, score_rows
from gitscout.storage import Store

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(**kw):
    base = {
        "login": "x",
        "kinds": "stars",
        "repo_count": 1,
        "followers": 0,
        "bio": None,
        "company": None,
        "location": None,
        "name": None,
        "last_seen": None,
        "email_conf": None,
    }
    base.update(kw)
    return base


def test_contributing_outranks_starring():
    pr = score_row(row(kinds="prs"), now=NOW)
    issue = score_row(row(kinds="issues"), now=NOW)
    fork = score_row(row(kinds="forks"), now=NOW)
    star = score_row(row(kinds="stars"), now=NOW)
    assert pr > issue > fork > star
    assert KIND_WEIGHT["prs"] > KIND_WEIGHT["stars"]


def test_multiple_signals_beat_one():
    assert score_row(row(kinds="stars,forks"), now=NOW) > score_row(row(kinds="forks"), now=NOW)


def test_repeated_signal_of_the_same_kind_adds_nothing_extra():
    assert score_row(row(kinds="stars,stars"), now=NOW) == score_row(row(kinds="stars"), now=NOW)


def test_touching_several_repos_helps():
    assert score_row(row(repo_count=3), now=NOW) > score_row(row(repo_count=1), now=NOW)


def test_icp_keywords_help_and_student_hurts():
    sec = score_row(row(bio="Cloud security engineer"), now=NOW)
    plain = score_row(row(bio="I like cats"), now=NOW)
    student = score_row(row(bio="student learning to code"), now=NOW)
    assert sec > plain >= student


def test_keywords_are_read_from_company_and_location_too():
    assert score_row(row(company="Acme DevSecOps"), now=NOW) > score_row(row(company="Acme"), now=NOW)


def test_followers_have_diminishing_returns():
    small = score_row(row(followers=10), now=NOW)
    big = score_row(row(followers=1_000), now=NOW)
    huge = score_row(row(followers=100_000), now=NOW)
    assert small < big < huge
    assert (huge - big) < (big - small)


def test_recency_decays():
    recent = score_row(row(last_seen=(NOW - timedelta(days=5)).isoformat()), now=NOW)
    old = score_row(row(last_seen=(NOW - timedelta(days=400)).isoformat()), now=NOW)
    ancient = score_row(row(last_seen=(NOW - timedelta(days=1500)).isoformat()), now=NOW)
    assert recent > old > ancient


def test_naive_and_malformed_timestamps_are_handled():
    assert score_row(row(last_seen="2025-12-20T00:00:00"), now=NOW) > 0  # no timezone
    assert score_row(row(last_seen="not a date"), now=NOW) >= 0
    assert score_row(row(last_seen=None), now=NOW) >= 0


def test_score_is_clamped_to_the_range():
    best = score_row(
        row(
            kinds="prs,issues,forks,stars",
            repo_count=99,
            followers=10**7,
            bio="cloud security devsecops kubernetes terraform aws sre",
            company="Acme",
            last_seen=NOW.isoformat(),
            email_conf=1.0,
        ),
        now=NOW,
    )
    worst = score_row(row(kinds=None, repo_count=0, bio="student"), now=NOW)
    assert 0.0 <= worst <= best <= 100.0


def test_score_rows_keys_by_login_and_skips_rows_without_one():
    scores = score_rows([row(login="a"), row(login=None)], now=NOW)
    assert list(scores) == ["a"]


def test_explain_components_sum_to_the_total():
    breakdown = explain(row(kinds="prs", followers=100, company="Acme"), now=NOW)
    total = breakdown.pop("total")
    assert abs(sum(breakdown.values()) - total) < 0.5


def test_rescore_writes_scores_for_real_users_only():
    with Store(":memory:") as store:
        store.add_interactions(
            [
                Interaction("o/r", "alice", "prs", "User", "2025-12-01"),
                Interaction("o/r", "bob", "stars", "User", "2020-01-01"),
                Interaction("o/r", "botty", "stars", "Bot", "2025-12-01"),
            ]
        )
        store.upsert_profile(Profile(login="alice", type="User", bio="cloud security", followers=500))
        store.upsert_profile(Profile(login="bob", type="User"))
        store.upsert_profile(Profile(login="botty", type="Bot"))
        store.add_emails("alice", [EmailCandidate("a@x.dev", "gql_profile", 0.95)])

        assert rescore(store, now=NOW) == 2  # the bot is excluded

        scores = {r["login"]: r["score"] for r in store.export_rows()}
        assert scores["alice"] > scores["bob"]
        assert "botty" not in scores


def test_landing_commits_is_the_strongest_signal():
    """A merged contributor outranks everyone, and discussions sit mid-table."""
    ordered = ["contribs", "prs", "issues", "discussions", "forks", "stars"]
    scores = [score_row(row(kinds=k), now=NOW) for k in ordered]
    assert scores == sorted(scores, reverse=True), dict(zip(ordered, scores))
    assert set(KIND_WEIGHT) == set(ALL_KINDS), "every kind needs a weight"
