"""Shared fixtures: an in-process fake GitHub API (httpx.MockTransport)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from gitscout.github_client import GitHubClient
from gitscout.graphql import GraphQLClient
from gqlhelpers import FakeGraphQL

BASE = "https://api.github.com"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeGitHub:
    """Static routes keyed by URL path. Query strings are ignored except `page`."""

    def __init__(self) -> None:
        self.routes: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.default_headers = {
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-reset": "9999999999",
        }

    # --- route builders
    def add(self, path: str, body: Any, status: int = 200, headers: dict | None = None) -> None:
        self.routes[path] = {"kind": "seq", "items": [(status, body, headers or {})], "i": 0}

    def add_sequence(self, path: str, items: list[tuple[int, Any, dict]]) -> None:
        self.routes[path] = {"kind": "seq", "items": items, "i": 0}

    def add_pages(self, path: str, pages: list[list[Any]]) -> None:
        self.routes[path] = {"kind": "pages", "pages": pages}

    # --- introspection
    def paths(self) -> list[str]:
        return [c[0] for c in self.calls]

    # --- transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.calls.append((path, query, dict(request.headers)))
        route = self.routes.get(path)
        headers = dict(self.default_headers)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)

        if route["kind"] == "seq":
            idx = min(route["i"], len(route["items"]) - 1)
            route["i"] += 1
            status, body, extra = route["items"][idx]
            headers.update(extra)
            return httpx.Response(status, json=body, headers=headers)

        pages = route["pages"]
        page = int(query.get("page", 1))
        if page > len(pages):
            return httpx.Response(200, json=[], headers=headers)
        if page < len(pages):
            next_query = dict(query, page=str(page + 1))
            headers["link"] = f'<{BASE}{path}?{urlencode(next_query)}>; rel="next"'
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def make_client():
    def factory(fake: FakeGitHub, tokens=("tok1",), clock: FakeClock | None = None, **kw):
        clock = clock or FakeClock()
        client = GitHubClient(
            tokens, transport=fake.transport, sleep=clock.sleep, clock=clock, **kw
        )
        client.fake_clock = clock  # type: ignore[attr-defined]
        return client

    return factory


@pytest.fixture
def gql() -> FakeGraphQL:
    return FakeGraphQL()


@pytest.fixture
def make_gql_client():
    """A GraphQLClient wired to a scripted fake endpoint, with a controllable clock."""

    def factory(fake: FakeGraphQL, tokens=("tok1",), clock: FakeClock | None = None, **kw):
        clock = clock or FakeClock()
        client = GraphQLClient(
            tokens, transport=fake.transport, sleep=clock.sleep, clock=clock, **kw
        )
        client.fake_clock = clock  # type: ignore[attr-defined]
        return client

    return factory


@pytest.fixture
def store():
    """A throwaway in-memory database."""
    from gitscout.storage import Store

    with Store(":memory:") as s:
        yield s


#: Every variable `load_settings` reads. A developer's real `.env` must never decide
#: whether a test passes -- before this, adding SUPABASE_URL locally broke two tests
#: that assert the unconfigured behaviour.
_LEAKY_ENV = (
    "GITHUB_TOKEN", "GITHUB_TOKENS", "GITSCOUT_DB", "GITSCOUT_TARGETS",
    "GITSCOUT_SCHEDULE", "GITSCOUT_CONCURRENCY", "GITSCOUT_PAGE_SIZE",
    "GITSCOUT_COMMIT_BATCH", "GITSCOUT_COMMITS_PER_REPO", "GITSCOUT_REPOS_PER_USER",
    "GITSCOUT_PROVIDER_BUDGET", "HUNTER_API_KEY", "APIFY_TOKEN", "APIFY_ACTOR",
    "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_SCHEMA",
    "SUPABASE_BATCH", "SUPABASE_DB_URL", "DATABASE_URL", "SUPABASE_DB_PASSWORD",
    "SUPABASE_PROJECT_REF", "SUPABASE_DB_HOST", "SUPABASE_DB_PORT",
)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """Run every test as if there were no .env and no exported credentials."""
    monkeypatch.setattr("gitscout.config.load_dotenv", lambda *a, **kw: False)
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
