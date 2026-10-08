"""The GraphQL client: error-in-200 handling, point accounting, pagination."""
from __future__ import annotations

import asyncio

import pytest
from gqlhelpers import DEFAULT_RATE_LIMIT, connection, error

from gitscout.github_client import GitHubAuthError
from gitscout.graphql import GraphQLClient, GraphQLError, NodeLimitExceeded, classify

PROBE = "query Probe($first: Int!, $after: String) { thing { nodes } }"


def run(coro):
    return asyncio.run(coro)


def test_classify_reads_types_then_falls_back_to_messages():
    assert classify([error("x", type_="NOT_FOUND")]) == {"NOT_FOUND"}
    assert classify([error("Something went wrong while executing your query")]) == {"RETRYABLE"}
    assert classify([error("API rate limit exceeded")]) == {"RATE_LIMITED"}
    assert classify([error("totally novel failure")]) == {"UNKNOWN"}
    assert classify([]) == set()


def test_execute_returns_data_and_accounts_for_points(gql, make_gql_client):
    gql.add("Probe", {"thing": {"ok": True}}, cost=7, remaining=4993)

    async def go():
        async with make_gql_client(gql) as client:
            result = await client.execute(PROBE)
            return result, client.points_used, client.queries_sent

    result, used, sent = run(go())
    assert result.pluck("thing", "ok") is True
    assert (result.cost, result.remaining) == (7, 4993)
    assert (used, sent) == (7, 1)


def test_pluck_returns_none_for_missing_hops(gql, make_gql_client):
    gql.add("Probe", {"thing": None})

    async def go():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE)

    result = run(go())
    assert result.pluck("thing", "nested", "deep") is None
    assert result.pluck("nope") is None


def test_token_is_sent_as_bearer(gql, make_gql_client):
    gql.add("Probe", {"thing": {}})

    async def go():
        async with make_gql_client(gql, tokens=("sekrit",)) as client:
            await client.execute(PROBE)

    run(go())
    assert gql.calls[0][2]["authorization"] == "Bearer sekrit"


def test_not_found_is_tolerated_by_default_but_can_be_strict(gql, make_gql_client):
    gql.add_errors("Probe", [error("Could not resolve to a Repository", type_="NOT_FOUND")])

    async def tolerant():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE)

    result = run(tolerant())
    assert result.errors and result.pluck("thing") is None

    gql.add_errors("Probe", [error("Could not resolve", type_="NOT_FOUND")])

    async def strict():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE, tolerate_missing=False)

    with pytest.raises(GraphQLError):
        run(strict())


def test_node_limit_raises_its_own_error(gql, make_gql_client):
    gql.add_errors("Probe", [error("too many nodes", type_="MAX_NODE_LIMIT_EXCEEDED")])

    async def go():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE)

    with pytest.raises(NodeLimitExceeded):
        run(go())


def test_unknown_error_is_fatal(gql, make_gql_client):
    gql.add_errors("Probe", [error("Field 'nope' doesn't exist on type 'Repository'")])

    async def go():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE)

    with pytest.raises(GraphQLError, match="doesn't exist"):
        run(go())


def test_retryable_error_is_retried_then_succeeds(gql, make_gql_client):
    gql.add_errors("Probe", [error("Something went wrong while executing your query")])
    gql.add("Probe", {"thing": {"ok": 1}})

    async def go():
        async with make_gql_client(gql) as client:
            result = await client.execute(PROBE)
            return result, client.fake_clock.slept

    result, slept = run(go())
    assert result.pluck("thing", "ok") == 1
    assert len(gql.calls) == 2 and slept  # it backed off before retrying


def test_rate_limited_sleeps_until_reset_then_retries(gql, make_gql_client):
    gql.add_errors(
        "Probe",
        [error("API rate limit exceeded", type_="RATE_LIMITED")],
        data={"rateLimit": {**DEFAULT_RATE_LIMIT, "cost": 0, "remaining": 0}},
    )
    gql.add("Probe", {"thing": {"ok": 2}})

    async def go():
        async with make_gql_client(gql) as client:
            result = await client.execute(PROBE)
            return result, client.fake_clock.slept

    result, slept = run(go())
    assert result.pluck("thing", "ok") == 2
    assert slept and slept[0] >= 60  # waited at least the floor


def test_401_raises_auth_error(gql, make_gql_client):
    gql.add_status("Probe", 401, "Bad credentials")

    async def go():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE)

    with pytest.raises(GitHubAuthError):
        run(go())


def test_server_error_is_retried(gql, make_gql_client):
    gql.add_status("Probe", 502)
    gql.add("Probe", {"thing": {"ok": 3}})

    async def go():
        async with make_gql_client(gql) as client:
            return await client.execute(PROBE)

    assert run(go()).pluck("thing", "ok") == 3
    assert len(gql.calls) == 2


def test_403_with_retry_after_backs_off(gql, make_gql_client):
    gql.add_body("Probe", {"message": "slow down"}, status=403)
    gql.add("Probe", {"thing": {"ok": 4}})

    async def go():
        async with make_gql_client(gql) as client:
            result = await client.execute(PROBE)
            return result, client.fake_clock.slept

    result, slept = run(go())
    assert result.pluck("thing", "ok") == 4 and slept


def test_give_up_after_max_retries(gql, make_gql_client):
    for _ in range(8):
        gql.add_status("Probe", 500)

    async def go():
        async with make_gql_client(gql, max_retries=2) as client:
            return await client.execute(PROBE)

    with pytest.raises(GraphQLError, match="after retries"):
        run(go())


# ------------------------------------------------------------------ pagination


def _page(n, has_next, cursor):
    return {"thing": connection(nodes=[{"i": n}], has_next=has_next, cursor=cursor)}


def test_paginate_follows_cursors_and_passes_them_back(gql, make_gql_client):
    gql.add("Probe", _page(1, True, "c1"))
    gql.add("Probe", _page(2, True, "c2"))
    gql.add("Probe", _page(3, False, None))

    async def go():
        async with make_gql_client(gql) as client:
            seen = []
            async for page in client.paginate(PROBE, {}, ("thing",)):
                seen.append((page.nodes[0]["i"], page.cursor, page.has_next))
            return seen

    assert run(go()) == [(1, "c1", True), (2, "c2", True), (3, None, False)]
    # the cursor from page N is what page N+1 asked for
    assert [v.get("after") for v in gql.variables_for("Probe")] == [None, "c1", "c2"]


def test_paginate_respects_max_pages(gql, make_gql_client):
    for i in range(5):
        gql.add("Probe", _page(i, True, f"c{i}"))

    async def go():
        async with make_gql_client(gql) as client:
            return [p.nodes[0]["i"] async for p in client.paginate(PROBE, {}, ("thing",), max_pages=2)]

    assert len(run(go())) == 2


def test_paginate_returns_nothing_when_connection_is_missing(gql, make_gql_client):
    gql.add("Probe", {"thing": None})

    async def go():
        async with make_gql_client(gql) as client:
            return [p async for p in client.paginate(PROBE, {}, ("thing",))]

    assert run(go()) == []


def test_paginate_halves_page_size_on_node_limit(gql, make_gql_client):
    gql.add_errors("Probe", [error("too many nodes", type_="MAX_NODE_LIMIT_EXCEEDED")])
    gql.add("Probe", _page(1, False, None))

    async def go():
        async with make_gql_client(gql) as client:
            return [p.nodes[0]["i"] async for p in client.paginate(PROBE, {}, ("thing",), page_size=100)]

    assert run(go()) == [1]
    assert [v["first"] for v in gql.variables_for("Probe")] == [100, 50]


def test_paginate_gives_up_when_even_one_node_is_too_many(gql, make_gql_client):
    for _ in range(10):
        gql.add_errors("Probe", [error("too many", type_="MAX_NODE_LIMIT_EXCEEDED")])

    async def go():
        async with make_gql_client(gql) as client:
            return [p async for p in client.paginate(PROBE, {}, ("thing",), page_size=2)]

    with pytest.raises(NodeLimitExceeded):
        run(go())


def test_paginate_stops_if_the_cursor_never_advances(gql, make_gql_client):
    """A connection that keeps replaying the same cursor must not loop forever."""
    gql.add("Probe", _page(1, True, "stuck"))  # sticky: the fake replays this page

    async def go():
        async with make_gql_client(gql) as client:
            return [p.nodes[0]["i"] async for p in client.paginate(PROBE, {}, ("thing",))]

    assert run(go()) == [1, 1]  # one real page, one repeat, then it gives up
