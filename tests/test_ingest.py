import asyncio

import pytest
from helpers import fork, issue, star

from gitscout.ingest import ingest_kind, parse_repo
from gitscout.storage import Store


def run(coro):
    return asyncio.run(coro)


def test_parse_repo():
    assert parse_repo("fastapi/fastapi") == "fastapi/fastapi"
    assert parse_repo("https://github.com/fastapi/fastapi") == "fastapi/fastapi"
    assert parse_repo("https://github.com/fastapi/fastapi/") == "fastapi/fastapi"
    assert parse_repo("github.com/fastapi/fastapi.git") == "fastapi/fastapi"
    assert parse_repo("https://github.com/o/r/issues/3") == "o/r"
    for bad in ("fastapi", "a/b/c", "", "not a repo", "../../etc/passwd"):
        with pytest.raises(ValueError):
            parse_repo(bad)


def test_ingest_stars_uses_star_media_type_and_paginates(fake, make_client):
    fake.add_pages("/repos/o/r/stargazers", [[star("alice"), star("bob")], [star("carol")]])

    async def go():
        with Store(":memory:") as store:
            async with make_client(fake) as client:
                res = await ingest_kind(client, store, "o/r", "stars")
            return res, store.stats()

    res, stats = run(go())
    assert (res.fetched, res.new, res.complete) == (3, 3, True)
    assert stats["by_kind"] == {"stars": 3}
    assert "star+json" in fake.calls[0][2]["accept"]


def test_ingest_forks_and_issues_filter_prs(fake, make_client):
    fake.add_pages("/repos/o/r/forks", [[fork("dan"), fork("erin")]])
    fake.add_pages(
        "/repos/o/r/issues",
        [[issue("frank", 1), issue("gina", 2, pr=True), issue("hank", 3)]],
    )

    async def go():
        with Store(":memory:") as store:
            async with make_client(fake) as client:
                await ingest_kind(client, store, "o/r", "forks")
                await ingest_kind(client, store, "o/r", "issues")
            return store.stats(), store.export_rows()

    stats, rows = run(go())
    assert stats["by_kind"] == {"forks": 2, "issues": 2}
    logins = {r["login"] for r in rows}
    assert logins == {"dan", "erin", "frank", "hank"}  # gina only opened a PR


def test_ingest_is_idempotent_and_skips_completed(fake, make_client):
    fake.add_pages("/repos/o/r/stargazers", [[star("alice")]])

    async def go():
        with Store(":memory:") as store:
            async with make_client(fake) as client:
                first = await ingest_kind(client, store, "o/r", "stars")
                second = await ingest_kind(client, store, "o/r", "stars")
                third = await ingest_kind(client, store, "o/r", "stars", fresh=True)
            return first, second, third, store.stats()["interactions"]

    first, second, third, total = run(go())
    assert first.new == 1
    assert second.skipped and second.fetched == 0
    assert third.fetched == 1 and third.new == 0  # fresh re-crawl adds no duplicates
    assert total == 1


def test_ingest_resumes_from_checkpoint(fake, make_client):
    fake.add_pages("/repos/o/r/stargazers", [[star("a"), star("b")], [star("c")]])

    async def go():
        with Store(":memory:") as store:
            async with make_client(fake) as client:
                partial = await ingest_kind(client, store, "o/r", "stars", max_items=1)
                calls_after_first = len(fake.calls)
                resumed = await ingest_kind(client, store, "o/r", "stars")
            return partial, resumed, calls_after_first, store.stats()["interactions"]

    partial, resumed, calls_after_first, total = run(go())
    assert not partial.complete and partial.fetched == 2  # soft cap rounds up to a page
    assert resumed.complete and resumed.fetched == 1  # only page 2 was fetched
    assert calls_after_first == 1 and len(fake.calls) == 2
    assert fake.calls[1][1]["page"] == "2"
    assert total == 3


def test_missing_repo_yields_nothing(fake, make_client):
    async def go():
        with Store(":memory:") as store:
            async with make_client(fake) as client:
                return await ingest_kind(client, store, "o/ghost", "stars")

    res = run(go())
    assert res.fetched == 0 and not res.complete
