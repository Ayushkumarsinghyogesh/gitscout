"""The Apify fallback: tolerant field mapping and clear failures."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gitscout.apify import (
    APIFY_CONFIDENCE,
    ApifyClient,
    ApifyError,
    actor_input,
    ingest_repos_apify,
    ingest_via_apify,
    map_item,
    pick,
)


def run(coro):
    return asyncio.run(coro)


def fake(items, status=200):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(status, json=items)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


# ------------------------------------------------------------- field mapping


def test_pick_accepts_the_many_spellings_actors_use():
    assert pick({"username": "alice"}, "login") == "alice"
    assert pick({"githubUsername": "bob"}, "login") == "bob"
    assert pick({"emailAddress": "x@y.dev"}, "email") == "x@y.dev"
    assert pick({"websiteUrl": "u"}, "blog") == "u"
    assert pick({"login": ""}, "login") is None  # blanks are not values
    assert pick({}, "login") is None


def test_map_item_builds_an_interaction_and_profile():
    mapped = map_item(
        "o/r",
        "stars",
        {
            "username": "@alice ",
            "emailAddress": "Alice@Acme.dev",
            "fullName": "Alice A",
            "organization": "Acme",
            "followersCount": "42",
            "website": "https://alice.dev",
        },
    )
    assert mapped is not None
    interaction, profile = mapped
    assert (interaction.login, interaction.kind) == ("alice", "stars")  # @ and spaces stripped
    assert (profile.name, profile.company, profile.followers) == ("Alice A", "Acme", 42)
    assert profile.blog == "https://alice.dev"


def test_map_item_rejects_unusable_rows():
    assert map_item("o/r", "stars", {}) is None
    assert map_item("o/r", "stars", {"login": "ghost"}) is None
    assert map_item("o/r", "stars", {"login": "   "}) is None
    assert map_item("o/r", "stars", {"login": 12345}) is None


def test_map_item_normalises_the_account_type():
    _, profile = map_item("o/r", "stars", {"login": "a", "type": "user"})
    assert profile.type == "User"
    _, org = map_item("o/r", "stars", {"login": "o", "type": "Organization"})
    assert org.type == "Organization"


def test_unparseable_follower_counts_do_not_crash():
    _, profile = map_item("o/r", "stars", {"login": "a", "followers": "lots"})
    assert profile.followers is None


def test_actor_input_sends_several_key_spellings():
    payload = actor_input("o/r", "stars")
    assert payload["repositoryUrl"] == "https://github.com/o/r"
    assert payload["repository"] == "o/r"
    assert payload["startUrls"][0]["url"].endswith("/stargazers")
    assert actor_input("o/r", "stars", {"maxItems": 5})["maxItems"] == 5


# ------------------------------------------------------------------- client


def test_token_is_required():
    with pytest.raises(ApifyError, match="APIFY_TOKEN"):
        ApifyClient("", actor="x")


def test_actor_slug_accepts_slash_or_tilde():
    assert ApifyClient("t", actor="user/actor").actor == "user~actor"
    assert ApifyClient("t", actor="user~actor").actor == "user~actor"


def test_run_actor_posts_to_the_sync_endpoint():
    http, seen = fake([{"login": "alice"}])

    async def go():
        async with ApifyClient("tok", actor="u/a", http=http) as client:
            return await client.run_actor({"repositoryUrl": "x"})

    items = run(go())
    assert items == [{"login": "alice"}]
    assert "run-sync-get-dataset-items" in seen["url"] and "token=tok" in seen["url"]


def test_run_actor_unwraps_an_items_envelope():
    http, _ = fake({"items": [{"login": "a"}], "count": 1})

    async def go():
        async with ApifyClient("tok", actor="u/a", http=http) as client:
            return await client.run_actor({})

    assert run(go()) == [{"login": "a"}]


def test_run_actor_errors_are_explicit():
    for status, match in ((401, "token"), (403, "token"), (404, "not found"), (500, "HTTP 500")):
        http, _ = fake({}, status=status)

        async def go(http=http):
            async with ApifyClient("tok", actor="u/a", http=http) as client:
                return await client.run_actor({})

        with pytest.raises(ApifyError, match=match):
            run(go())


def test_non_json_response_is_an_error():
    def handler(request):
        return httpx.Response(200, text="<html>nope</html>")

    async def go():
        async with ApifyClient(
            "tok", actor="u/a", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            return await client.run_actor({})

    with pytest.raises(ApifyError, match="non-JSON"):
        run(go())


# -------------------------------------------------------------------- ingest


def test_ingest_folds_results_into_the_database(store):
    http, _ = fake(
        [
            {"username": "alice", "email": "alice@acme.dev"},
            {"username": "bob"},
            {"nothing": "useful"},
            {"username": "spammy", "email": "noreply@github.com"},
        ]
    )

    async def go():
        async with ApifyClient("tok", actor="u/a", http=http) as client:
            return await ingest_via_apify(client, store, "https://github.com/o/r")

    res = run(go())
    assert (res.items, res.fetched, res.new) == (4, 3, 3)  # the junk row is dropped
    assert res.emails_new == 1  # the noreply address is filtered out
    row = next(r for r in store.export_rows() if r["login"] == "alice")
    assert row["email"] == "alice@acme.dev"
    assert row["email_source"] == "apify"
    assert row["email_confidence"] == APIFY_CONFIDENCE


def test_apify_confidence_sits_below_github_sources():
    from gitscout.emails import SOURCE_BASE_CONFIDENCE

    assert APIFY_CONFIDENCE < SOURCE_BASE_CONFIDENCE["commit_api"]


def test_ingest_is_idempotent(store):
    http, _ = fake([{"username": "alice", "email": "alice@acme.dev"}])

    async def go():
        async with ApifyClient("tok", actor="u/a", http=http) as client:
            first = await ingest_via_apify(client, store, "o/r")
            second = await ingest_via_apify(client, store, "o/r")
            return first, second

    first, second = run(go())
    assert first.new == 1 and second.new == 0
    assert second.emails_new == 0


def test_ingest_many_repos_and_kinds(store):
    http, _ = fake([{"username": "alice"}])

    async def go():
        async with ApifyClient("tok", actor="u/a", http=http) as client:
            return await ingest_repos_apify(client, store, ["o/a", "o/b"], ["stars", "forks"])

    results = run(go())
    assert len(results) == 4
    assert store.stats()["repos"] == 2
