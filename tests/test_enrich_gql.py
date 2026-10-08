"""Commit-email discovery: attribution rules, batching, and fallbacks."""
from __future__ import annotations

import asyncio

import httpx
from gqlhelpers import commit, commit_probe, error, gql_user, stars_body

from gitscout.enrich_gql import GqlEnricher, commit_candidates
from gitscout.models import Profile
from gitscout.queries import commit_email_query
from gitscout.web import WebsiteScanner


def run(coro):
    return asyncio.run(coro)


def _seed(store, logins, **profile_kw):
    for login in logins:
        store.upsert_profile(Profile(login=login, type="User", **profile_kw))


def _enrich(gql, make_gql_client, store, **kw):
    async def go():
        async with make_gql_client(gql) as client:
            async with GqlEnricher(client, store, **kw) as enricher:
                return await enricher.run()

    return run(go())


# ------------------------------------------------------- attribution rules


def test_linked_login_scores_full_confidence():
    probe = commit_probe("alice", repos=[[commit("alice@acme.dev", name="Alice", linked="alice")]])
    found = commit_candidates(Profile(login="alice"), probe)
    assert [(c.email, c.source, c.confidence) for c in found] == [
        ("alice@acme.dev", "gql_commit", 0.85)
    ]


def test_unlinked_commit_with_matching_name_is_halved():
    probe = commit_probe("bob", name="Bob Brown", repos=[[commit("bob@bob.dev", name="Bob Brown")]])
    found = commit_candidates(Profile(login="bob", name="Bob Brown"), probe)
    assert [(c.email, c.confidence) for c in found] == [("bob@bob.dev", 0.42)]


def test_unrelated_author_is_dropped():
    """A commit by someone else in your repo is not your address."""
    probe = commit_probe("carol", name="Carol C", repos=[[commit("stranger@x.dev", name="Totally Else")]])
    assert commit_candidates(Profile(login="carol", name="Carol C"), probe) == []


def test_commit_linked_to_a_different_user_is_dropped():
    probe = commit_probe("dave", repos=[[commit("other@x.dev", name="Other", linked="someoneelse")]])
    assert commit_candidates(Profile(login="dave"), probe) == []


def test_noreply_and_junk_are_filtered():
    probe = commit_probe(
        "erin",
        repos=[[commit("1+erin@users.noreply.github.com", linked="erin"), commit("x@example.com", linked="erin")]],
    )
    assert commit_candidates(Profile(login="erin"), probe) == []


def test_strong_matches_outrank_weak_ones_and_are_capped_at_two():
    probe = commit_probe(
        "frank",
        name="Frank F",
        repos=[
            [commit("a@acme.dev", linked="frank"), commit("b@acme.dev", linked="frank")],
            [commit("c@acme.dev", linked="frank"), commit("weak@x.dev", name="Frank F")],
        ],
    )
    found = commit_candidates(Profile(login="frank", name="Frank F"), probe)
    assert len(found) == 2
    assert all(c.confidence == 0.85 for c in found)


def test_weak_fills_remaining_slots():
    probe = commit_probe(
        "gina",
        name="Gina G",
        repos=[[commit("strong@acme.dev", linked="gina"), commit("weak@x.dev", name="Gina G")]],
    )
    found = commit_candidates(Profile(login="gina", name="Gina G"), probe)
    assert [c.confidence for c in found] == [0.85, 0.42]


def test_empty_and_malformed_probes_are_safe():
    assert commit_candidates(Profile(login="x"), None) == []
    assert commit_candidates(Profile(login="x"), {}) == []
    assert commit_candidates(Profile(login="x"), {"login": "x", "repositories": None}) == []
    assert commit_candidates(
        Profile(login="x"),
        {"login": "x", "repositories": {"nodes": [None, {"defaultBranchRef": None}]}},
    ) == []


def test_non_commit_target_is_ignored():
    """An empty repo's defaultBranchRef target has no history."""
    probe = {
        "login": "x",
        "repositories": {"nodes": [{"defaultBranchRef": {"target": {"__typename": "Tree"}}}]},
    }
    assert commit_candidates(Profile(login="x"), probe) == []


# --------------------------------------------------------------- query building


def test_commit_query_passes_logins_as_variables_not_interpolation():
    """Logins are attacker-influenced; they must never land in the document text."""
    query, variables = commit_email_query(['evil") { x } #'])
    assert 'evil' not in query
    assert variables == {"l0": 'evil") { x } #'}
    assert "$l0: String!" in query and "u0: user(login: $l0)" in query


def test_commit_query_aliases_every_login():
    query, variables = commit_email_query(["a", "b", "c"])
    assert variables == {"l0": "a", "l1": "b", "l2": "c"}
    for i in range(3):
        assert f"u{i}: user(login: $l{i})" in query


# -------------------------------------------------------------------- batching


def test_one_query_covers_the_whole_batch(gql, make_gql_client, store):
    logins = [f"u{i}" for i in range(10)]
    _seed(store, logins)
    gql.add(
        "CommitEmails",
        {f"u{i}": commit_probe(lg, repos=[[commit(f"{lg}@acme.dev", linked=lg)]]) for i, lg in enumerate(logins)},
    )

    summary = _enrich(gql, make_gql_client, store, batch_size=10)

    assert (summary.processed, summary.with_email, summary.queries) == (10, 10, 1)
    assert summary.emails_new == 10 and summary.points == 1  # 10 users for one point
    assert len(gql.calls) == 1
    assert store.stats()["users_with_email"] == 10


def test_batching_splits_into_several_queries(gql, make_gql_client, store):
    logins = [f"u{i}" for i in range(7)]
    _seed(store, logins)
    gql.add("CommitEmails", {})
    summary = _enrich(gql, make_gql_client, store, batch_size=3)
    assert summary.queries == 3  # 3 + 3 + 1
    assert summary.processed == 7


def test_users_are_marked_probed_so_a_rerun_skips_them(gql, make_gql_client, store):
    _seed(store, ["alice"])
    gql.add("CommitEmails", {"u0": commit_probe("alice", repos=[[]])})

    first = _enrich(gql, make_gql_client, store)
    assert first.processed == 1 and first.with_email == 0

    second = _enrich(gql, make_gql_client, store)
    assert second.processed == 0  # no email found, but we do not re-probe
    assert store.profiles(["alice"])  # still present


def test_users_who_already_have_an_email_are_not_probed(gql, make_gql_client, store, ):
    from gitscout.models import EmailCandidate

    _seed(store, ["alice"])
    store.add_emails("alice", [EmailCandidate("alice@acme.dev", "gql_profile", 0.95)])
    summary = _enrich(gql, make_gql_client, store)
    assert summary.processed == 0 and len(gql.calls) == 0


def test_bots_and_missing_profiles_are_never_probed(gql, make_gql_client, store):
    store.upsert_profile(Profile(login="botty", type="Bot"))
    store.upsert_profile(Profile(login="gone", type="Missing", found=False))
    summary = _enrich(gql, make_gql_client, store)
    assert summary.processed == 0


def test_node_limit_falls_back_to_one_user_at_a_time(gql, make_gql_client, store):
    _seed(store, ["a", "b"])
    gql.add_errors("CommitEmails", [error("too many nodes", type_="MAX_NODE_LIMIT_EXCEEDED")])
    gql.add("CommitEmails", {"u0": commit_probe("a", repos=[[commit("a@acme.dev", linked="a")]])})
    gql.add("CommitEmails", {"u0": commit_probe("b", repos=[[commit("b@acme.dev", linked="b")]])})

    summary = _enrich(gql, make_gql_client, store, batch_size=2)
    # the rejected batch never ran, so only the two single-user queries count
    assert summary.with_email == 2 and summary.queries == 2
    assert store.stats()["users_with_email"] == 2


def test_limit_caps_the_number_of_users(gql, make_gql_client, store):
    _seed(store, [f"u{i}" for i in range(5)])
    gql.add("CommitEmails", {})

    async def go():
        async with make_gql_client(gql) as client:
            async with GqlEnricher(client, store, batch_size=10) as enricher:
                return await enricher.run(limit=2)

    assert run(go()).processed == 2


# -------------------------------------------------------------- website fallback


def _site(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_website_scan_finds_addresses_for_users_still_missing_one(gql, make_gql_client, store):
    from gitscout.models import Interaction

    store.add_interactions([Interaction("o/r", "carol", "stars", "User", "2024-01-01T00:00:00Z")])
    _seed(store, ["carol"], blog="https://carol.dev")
    gql.add("CommitEmails", {"u0": commit_probe("carol", repos=[[]])})

    def handler(request):
        return httpx.Response(200, text="reach me at hi@carol.dev", headers={"content-type": "text/html"})

    async def allow(host):
        return True

    async def go():
        scanner = WebsiteScanner(_site(handler), host_check=allow)
        async with make_gql_client(gql) as client:
            async with GqlEnricher(
                client, store, scan_websites=True, scanner=scanner
            ) as enricher:
                summary = await enricher.run()
        await scanner.aclose()
        return summary

    summary = run(go())
    assert summary.websites_scanned == 1 and summary.emails_new == 1
    row = next(r for r in store.export_rows() if r["login"] == "carol")
    assert row["email"] == "hi@carol.dev" and row["email_source"] == "website"


def test_website_scan_is_skipped_unless_asked_for(gql, make_gql_client, store):
    _seed(store, ["carol"], blog="https://carol.dev")
    gql.add("CommitEmails", {"u0": commit_probe("carol", repos=[[]])})
    summary = _enrich(gql, make_gql_client, store)
    assert summary.websites_scanned == 0
