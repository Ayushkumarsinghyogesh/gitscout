import asyncio

import pytest

from gitscout.github_client import GitHubAuthError, GitHubError


def run(coro):
    return asyncio.run(coro)


def test_pagination_follows_link_header(fake, make_client):
    fake.add_pages("/things", [[1, 2], [3, 4], [5]])

    async def go():
        async with make_client(fake) as client:
            out = []
            async for items, next_url in client.pages("/things", {"per_page": 2}):
                out.extend(items)
            return out

    assert run(go()) == [1, 2, 3, 4, 5]


def test_pages_can_resume_from_start_url(fake, make_client):
    fake.add_pages("/things", [[1], [2], [3]])

    async def go():
        async with make_client(fake) as client:
            out = []
            async for items, _ in client.pages(
                "/things", start_url="https://api.github.com/things?page=2"
            ):
                out.extend(items)
            return out

    assert run(go()) == [2, 3]


def test_404_returns_none(fake, make_client):
    async def go():
        async with make_client(fake) as client:
            return await client.get_json("/nope")

    assert run(go()) is None


def test_tokens_are_rotated(fake, make_client):
    fake.add("/x", {"ok": True})

    async def go():
        async with make_client(fake, tokens=("tokA", "tokB")) as client:
            for _ in range(4):
                await client.get_json("/x")

    run(go())
    auth = [c[2].get("authorization") for c in fake.calls]
    assert set(auth) == {"Bearer tokA", "Bearer tokB"}
    assert auth[0] != auth[1]


def test_no_token_means_no_auth_header(fake, make_client):
    fake.add("/x", {"ok": True})

    async def go():
        async with make_client(fake, tokens=()) as client:
            await client.get_json("/x")

    run(go())
    assert "authorization" not in fake.calls[0][2]


def test_primary_rate_limit_waits_for_reset(fake, make_client):
    limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1010"}
    fake.add_sequence(
        "/x", [(403, {"message": "API rate limit exceeded"}, limited), (200, {"ok": True}, {})]
    )

    async def go():
        client = make_client(fake)
        async with client:
            data = await client.get_json("/x")
        return data, client.fake_clock.slept

    data, slept = run(go())
    assert data == {"ok": True}
    assert slept == [11.0]  # reset(1010) - now(1000) + 1


def test_secondary_rate_limit_honours_retry_after(fake, make_client):
    fake.add_sequence(
        "/x",
        [
            (403, {"message": "secondary rate limit"}, {"retry-after": "5", "x-ratelimit-remaining": "50"}),
            (200, {"ok": 1}, {}),
        ],
    )

    async def go():
        client = make_client(fake)
        async with client:
            await client.get_json("/x")
        return client.fake_clock.slept

    assert run(go()) == [6.0]


def test_server_errors_are_retried(fake, make_client):
    fake.add_sequence("/x", [(502, {}, {}), (200, {"ok": 1}, {})])

    async def go():
        client = make_client(fake)
        async with client:
            data = await client.get_json("/x")
        return data, client.fake_clock.slept

    data, slept = run(go())
    assert data == {"ok": 1}
    assert slept == [2]


def test_server_errors_eventually_raise(fake, make_client):
    fake.add("/x", {}, status=500)

    async def go():
        async with make_client(fake, max_retries=2) as client:
            await client.get_json("/x")

    with pytest.raises(GitHubError):
        run(go())


def test_bad_credentials_raise_auth_error(fake, make_client):
    fake.add("/x", {"message": "Bad credentials"}, status=401)

    async def go():
        async with make_client(fake) as client:
            await client.get_json("/x")

    with pytest.raises(GitHubAuthError):
        run(go())


def test_refuses_to_send_token_to_other_hosts(fake, make_client):
    async def go():
        async with make_client(fake) as client:
            await client.get_json("https://evil.example/steal")

    with pytest.raises(GitHubError):
        run(go())
    assert fake.calls == []


def test_rate_limits_report(fake, make_client):
    fake.add(
        "/rate_limit",
        {"resources": {"core": {"limit": 5000, "remaining": 4321, "reset": 123}}},
    )

    async def go():
        async with make_client(fake, tokens=("ghp_abcdefghijklmnop",)) as client:
            return await client.rate_limits()

    rows = run(go())
    assert rows[0]["remaining"] == 4321
    assert "abcdefghijklmnop" not in rows[0]["token"]  # token is masked
