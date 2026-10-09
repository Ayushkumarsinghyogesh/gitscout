"""Stars via the events API — the only route left after GitHub's 2026-06-30 change."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from gqlhelpers import DEFAULT_RATE_LIMIT, gql_bot, gql_user, op_name, watch_event

from gitscout.config import Settings
from gitscout.github_client import GitHubClient
from gitscout.ingest_events import (
    EVENTS_PER_PAGE,
    MAX_EVENT_PAGES,
    ingest_stars_via_events,
    star_events,
)
from gitscout.ingest_gql import EmailScope
from gitscout.pipeline import run_scout
from gitscout.targets import targets_from_repos


def run(coro):
    return asyncio.run(coro)


def rest_fake(pages):
    """Serve /events pages; `pages` is a list of event lists."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params).get("page", 1))
        calls.append(page)
        body = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(
            200, json=body, headers={"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}
        )

    return handler, calls


def rest_client(handler, **kw):
    return GitHubClient(("tok",), transport=httpx.MockTransport(handler), **kw)


# ------------------------------------------------------------------ star_events


def test_only_watch_events_are_kept():
    handler, _ = rest_fake(
        [
            [
                watch_event("alice", "2026-10-08T10:00:00Z"),
                {"type": "PushEvent", "created_at": "2026-10-08T09:00:00Z", "actor": {"login": "coder"}},
                {"type": "ForkEvent", "created_at": "2026-10-08T08:00:00Z", "actor": {"login": "forker"}},
                watch_event("bob", "2026-10-08T07:00:00Z"),
            ]
        ]
    )

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r")

    events, stopped = run(go())
    assert [(e.login, e.starred_at) for e in events] == [
        ("alice", "2026-10-08T10:00:00Z"),
        ("bob", "2026-10-08T07:00:00Z"),
    ]
    assert stopped is False


def test_duplicate_and_ghost_actors_are_dropped():
    handler, _ = rest_fake(
        [
            [
                watch_event("alice", "2026-10-08T10:00:00Z"),
                watch_event("alice", "2026-10-08T09:00:00Z"),  # starred, unstarred, re-starred
                watch_event("ghost", "2026-10-08T08:00:00Z"),
                {"type": "WatchEvent", "created_at": "x", "actor": {"login": ""}},
                {"type": "WatchEvent", "created_at": "x"},  # no actor at all
            ]
        ]
    )

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r")

    events, _ = run(go())
    assert [e.login for e in events] == ["alice"]


def test_paging_stops_at_the_high_water_mark():
    """The cron path: read page 1, halt at the first star already recorded."""
    handler, calls = rest_fake(
        [
            [
                watch_event("new2", "2026-10-08T12:00:00Z"),
                watch_event("new1", "2026-10-08T11:00:00Z"),
                watch_event("seen", "2026-10-08T10:00:00Z"),  # <- stop here
                watch_event("older", "2026-10-08T09:00:00Z"),
            ],
            [watch_event("ancient", "2026-10-01T00:00:00Z")],
        ]
    )

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r", stop_at="2026-10-08T10:00:00Z")

    events, stopped = run(go())
    assert [e.login for e in events] == ["new2", "new1"]
    assert stopped is True
    assert calls == [1]  # page 2 was never requested


def test_paging_walks_up_to_the_api_ceiling():
    full = [watch_event(f"u{p}_{i}", f"2026-10-0{p}T0{i}:00:00Z") for p in (1,) for i in range(1)]
    pages = [[watch_event(f"p{p}", f"2026-10-08T0{p}:00:00Z")] * 1 + [{"type": "PushEvent"}] * (EVENTS_PER_PAGE - 1) for p in range(1, 6)]
    handler, calls = rest_fake(pages)

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r")

    events, _ = run(go())
    assert calls == list(range(1, MAX_EVENT_PAGES + 1))  # never more than the ceiling
    assert len(events) == MAX_EVENT_PAGES
    assert full == full  # (keeps the builder referenced for readability)


def test_a_short_page_ends_pagination():
    handler, calls = rest_fake([[watch_event("alice")]])

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r")

    run(go())
    assert calls == [1]


def test_a_non_list_response_does_not_crash():
    """GitHub answers with an object for the 'pagination is limited' notice."""

    def handler(request):
        return httpx.Response(200, json={"message": "pagination is limited"})

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r")

    events, _ = run(go())
    assert events == []


def test_a_missing_repo_yields_nothing():
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    async def go():
        async with rest_client(handler) as c:
            return await star_events(c, "o/r")

    assert run(go()) == ([], False)


# ----------------------------------------------------- full events-based ingest


def combined_fake(events, profiles):
    """One transport serving the REST events endpoint and the GraphQL Profiles query."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            page = int(dict(request.url.params).get("page", 1))
            return httpx.Response(200, json=events if page == 1 else [])
        name = op_name(json.loads(request.content or b"{}").get("query", ""))
        data = dict(profiles) if name == "Profiles" else {}
        data["rateLimit"] = DEFAULT_RATE_LIMIT
        return httpx.Response(200, json={"data": data})

    return httpx.MockTransport(handler)


def test_ingest_records_stars_with_full_profiles(store):
    transport = combined_fake(
        [watch_event("alice", "2026-10-08T10:00:00Z"), watch_event("bob", "2026-10-08T09:00:00Z")],
        {
            "u0": gql_user("alice", name="Alice", email="alice@acme.dev", company="Acme"),
            "u1": gql_user("bob", name="Bob"),
        },
    )
    from gitscout.graphql import GraphQLClient

    async def go():
        async with GitHubClient(("tok",), transport=transport) as rest:
            async with GraphQLClient(("tok",), transport=transport) as gql:
                return await ingest_stars_via_events(rest, gql, store, "o/r")

    res = run(go())
    assert (res.fetched, res.new, res.profiles) == (2, 2, 2)
    assert res.emails_new == 1  # only alice published an address
    assert res.points == 1  # both profiles in one batched query
    assert store.stats()["by_kind"] == {"stars": 2}
    row = next(r for r in store.export_rows() if r["login"] == "alice")
    assert row["email"] == "alice@acme.dev" and row["company"] == "Acme"


def test_bots_are_recorded_but_not_exported(store):
    transport = combined_fake(
        [watch_event("ci-bot", "2026-10-08T10:00:00Z"), watch_event("alice", "2026-10-08T09:00:00Z")],
        {"u0": gql_bot("ci-bot"), "u1": gql_user("alice")},
    )
    from gitscout.graphql import GraphQLClient

    async def go():
        async with GitHubClient(("tok",), transport=transport) as rest:
            async with GraphQLClient(("tok",), transport=transport) as gql:
                return await ingest_stars_via_events(rest, gql, store, "o/r")

    res = run(go())
    assert res.fetched == 2
    assert [r["login"] for r in store.export_rows()] == ["alice"]


def test_incremental_run_remembers_the_high_water_mark(store):
    events = [watch_event("alice", "2026-10-08T10:00:00Z")]
    transport = combined_fake(events, {"u0": gql_user("alice")})
    from gitscout.graphql import GraphQLClient

    async def go(incremental):
        async with GitHubClient(("tok",), transport=transport) as rest:
            async with GraphQLClient(("tok",), transport=transport) as gql:
                return await ingest_stars_via_events(
                    rest, gql, store, "o/r", incremental=incremental
                )

    first = run(go(False))
    assert first.new == 1
    assert store.get_crawl_state("events:o/r:stars")["high_water"] == "2026-10-08T10:00:00Z"

    second = run(go(True))
    assert (second.new, second.fetched, second.stopped_early) == (0, 0, True)
    assert second.points == 0  # no profiles to look up, so nothing was spent


def test_missing_profile_still_records_the_interaction(store):
    """A deleted account returns no node; the star itself is still a fact."""
    transport = combined_fake([watch_event("vanished", "2026-10-08T10:00:00Z")], {})
    from gitscout.graphql import GraphQLClient

    async def go():
        async with GitHubClient(("tok",), transport=transport) as rest:
            async with GraphQLClient(("tok",), transport=transport) as gql:
                return await ingest_stars_via_events(rest, gql, store, "o/r")

    res = run(go())
    assert (res.new, res.profiles) == (1, 0)


def test_scope_downgrade_is_honoured(store):
    """Without read:user the profile query drops `email` rather than failing."""
    import json

    scope_error = {
        "message": "Your token has not been granted the required scopes to execute this "
        "query. The 'email' field requires one of the following scopes: ['read:user']"
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            page = int(dict(request.url.params).get("page", 1))
            return httpx.Response(
                200, json=[watch_event("alice", "2026-10-08T10:00:00Z")] if page == 1 else []
            )
        query = json.loads(request.content or b"{}").get("query", "")
        if "\n  email\n" in query:
            return httpx.Response(200, json={"data": None, "errors": [scope_error]})
        return httpx.Response(
            200,
            json={"data": {"u0": gql_user("alice"), "rateLimit": DEFAULT_RATE_LIMIT}},
        )

    from gitscout.graphql import GraphQLClient

    transport = httpx.MockTransport(handler)
    scope = EmailScope()

    async def go():
        async with GitHubClient(("tok",), transport=transport) as rest:
            async with GraphQLClient(("tok",), transport=transport) as gql:
                return await ingest_stars_via_events(rest, gql, store, "o/r", scope=scope)

    res = run(go())
    assert res.new == 1 and scope.downgraded is True


def test_pipeline_routes_stars_through_events_and_other_kinds_through_graphql(tmp_path):
    """`-k stars,issues` must use both paths in one run."""
    import json

    from gqlhelpers import authored_body

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            page = int(dict(request.url.params).get("page", 1))
            return httpx.Response(
                200, json=[watch_event("stargazer", "2026-10-08T10:00:00Z")] if page == 1 else []
            )
        name = op_name(json.loads(request.content or b"{}").get("query", ""))
        bodies = {
            "Issues": authored_body("issues", [(gql_user("reporter"), "2026-10-07T00:00:00Z")]),
            "Profiles": {"u0": gql_user("stargazer", email="star@acme.dev")},
        }
        data = dict(bodies.get(name, {}))
        data["rateLimit"] = DEFAULT_RATE_LIMIT
        return httpx.Response(200, json={"data": data})

    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "t.db"))
    result = run(
        run_scout(
            settings,
            targets_from_repos(["o/r"], ("stars", "issues")),
            transport=httpx.MockTransport(handler),
        )
    )
    kinds = {r.kind for r in result.ingest.results}
    assert kinds == {"stars", "issues"}
    assert result.ingest.new == 2

    from gitscout.storage import Store

    with Store(settings.db_path) as store:
        assert store.stats()["by_kind"] == {"issues": 1, "stars": 1}
        assert next(r for r in store.export_rows() if r["login"] == "stargazer")["email"] == "star@acme.dev"
