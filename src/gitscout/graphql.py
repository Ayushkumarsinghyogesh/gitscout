"""Async GitHub GraphQL client: token rotation, point accounting, cursor pagination.

GraphQL differs from REST in three ways that shape this module:

* **Errors arrive inside HTTP 200.** A response may carry partial ``data`` *and* an
  ``errors`` array, so status codes alone tell you nothing.
* **The budget is points, not requests.** 5,000 points/hour, where cost is
  (requests needed per connection) / 100, rounded, minimum 1. One query returning
  100 stargazers *with their full profiles* costs ~1 point; the REST equivalent is
  101 requests. That ~100x gap is the whole reason this module exists.
* **REST and GraphQL budgets are separate**, so this client owns its own TokenPool
  rather than sharing one with GitHubClient.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Sequence

import httpx

from .github_client import GitHubAuthError, GitHubError, SleepFn, TokenPool

log = logging.getLogger(__name__)

DEFAULT_GRAPHQL_URL = "https://api.github.com/graphql"

#: Error types meaning "the thing does not exist" rather than "the query failed".
MISSING_TYPES = frozenset({"NOT_FOUND"})
#: Error types meaning "ask for less and try again".
NODE_LIMIT_TYPES = frozenset({"MAX_NODE_LIMIT_EXCEEDED"})
#: Error types meaning "out of budget".
RATE_LIMIT_TYPES = frozenset({"RATE_LIMITED"})

#: GraphQL's ``User.email`` needs the ``read:user`` (or ``user:email``) scope. A token
#: without it fails the *whole* query -- and only when a user actually has an address
#: set, so the failure is data-dependent and shows up mid-crawl. Detected here so the
#: caller can fall back to a query that omits the field.
_SCOPE_MESSAGES = (
    "not been granted the required scopes",
    "requires one of the following scopes",
)

_RETRYABLE_MESSAGES = (
    "something went wrong while executing your query",
    "timeout",
    "timedout",
    "bad gateway",
    "loading",
)


class GraphQLError(GitHubError):
    """A GraphQL query came back with errors we cannot recover from."""

    def __init__(self, message: str, errors: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.errors = list(errors)


class NodeLimitExceeded(GraphQLError):
    """The query asked for too many nodes; retry with a smaller page size."""


class MissingScopeError(GraphQLError):
    """The token lacks a scope a requested field needs (in practice: `read:user`).

    Recoverable by asking for less: see `EmailScope` in [ingest_gql.py], which drops
    the `email` field and re-runs the crawl rather than failing the whole job.
    """


@dataclass
class QueryResult:
    """One executed GraphQL query."""

    data: dict[str, Any]
    cost: int = 0
    remaining: int | None = None
    reset_at: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)

    def pluck(self, *path: str) -> Any:
        """Walk ``path`` through the response, returning None if any hop is missing."""
        node: Any = self.data
        for key in path:
            if not isinstance(node, Mapping):
                return None
            node = node.get(key)
            if node is None:
                return None
        return node


@dataclass
class Page:
    """One page of a GraphQL connection.

    ``parent`` is the object the connection hangs off (e.g. ``repository``), so callers
    can read sibling scalars selected alongside it -- ``stargazerCount`` next to
    ``stargazers``, for instance.
    """

    connection: dict[str, Any]
    cost: int
    cursor: str | None
    has_next: bool
    parent: dict[str, Any] = field(default_factory=dict)

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return [n for n in (self.connection.get("nodes") or []) if n]

    @property
    def edges(self) -> list[dict[str, Any]]:
        return [e for e in (self.connection.get("edges") or []) if e]


def classify(errors: Sequence[Mapping[str, Any]]) -> set[str]:
    """Collapse an ``errors`` array into the set of error *types* it contains."""
    out: set[str] = set()
    for err in errors:
        etype = err.get("type")
        if etype:
            out.add("MISSING_SCOPE" if etype == "INSUFFICIENT_SCOPES" else str(etype))
            continue
        message = str(err.get("message", "")).lower()
        if any(frag in message for frag in _SCOPE_MESSAGES):
            out.add("MISSING_SCOPE")
        elif any(frag in message for frag in _RETRYABLE_MESSAGES):
            out.add("RETRYABLE")
        elif "rate limit" in message:
            out.add("RATE_LIMITED")
        else:
            out.add("UNKNOWN")
    return out


class GraphQLClient:
    """POSTs GraphQL documents to GitHub, with retries and a point budget."""

    def __init__(
        self,
        tokens: Iterable[str] = (),
        *,
        url: str = DEFAULT_GRAPHQL_URL,
        user_agent: str = "gitscout/0.2",
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 5,
        timeout: float = 60.0,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = url
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self.pool = TokenPool(tokens, clock=clock, sleep=sleep)
        self.points_used = 0
        self.queries_sent = 0
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": user_agent,
            },
        )

    async def __aenter__(self) -> "GraphQLClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        tolerate_missing: bool = True,
    ) -> QueryResult:
        """Run one document. Raises on fatal errors; tolerates NOT_FOUND by default."""
        attempt = 0
        budget_waits = 0
        payload = {"query": query, "variables": dict(variables or {})}

        while True:
            state = await self.pool.acquire()
            headers = {"Authorization": f"Bearer {state.token}"} if state.token else {}
            try:
                resp = await self._http.post(self._url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise GraphQLError(f"Network error calling GraphQL: {exc}") from exc
                await self._sleep(min(2**attempt, 30))
                continue

            self.pool.update(state, resp.headers)

            if resp.status_code == 401:
                raise GitHubAuthError(f"Bad credentials for token {state.label}")
            if resp.status_code in (403, 429):
                attempt += 1
                if attempt > self._max_retries:
                    raise GraphQLError(f"GraphQL refused the request: {resp.text[:200]}")
                wait = _retry_after(resp)
                log.warning("GraphQL %s; sleeping %.0fs", resp.status_code, wait)
                await self._sleep(wait)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > self._max_retries:
                    raise GraphQLError(f"HTTP {resp.status_code} from GraphQL after retries")
                await self._sleep(min(2**attempt, 30))
                continue
            if resp.status_code != 200:
                raise GraphQLError(f"HTTP {resp.status_code} from GraphQL: {resp.text[:200]}")

            try:
                body = resp.json()
            except ValueError as exc:
                raise GraphQLError(f"GraphQL returned non-JSON: {resp.text[:200]}") from exc

            self.queries_sent += 1
            data = body.get("data") or {}
            errors = list(body.get("errors") or [])
            result = self._with_budget(data, errors)

            if not errors:
                return result

            kinds = classify(errors)
            first = str(errors[0].get("message", "GraphQL error"))

            if kinds & NODE_LIMIT_TYPES:
                raise NodeLimitExceeded(first, errors)
            if "MISSING_SCOPE" in kinds:
                raise MissingScopeError(first, errors)
            if kinds & RATE_LIMIT_TYPES:
                budget_waits += 1
                if budget_waits > 10:
                    raise GraphQLError("GraphQL point budget never recovered", errors)
                await self._wait_for_budget(result)
                continue
            if "RETRYABLE" in kinds:
                attempt += 1
                if attempt > self._max_retries:
                    raise GraphQLError(f"GraphQL kept failing: {first}", errors)
                log.warning("Retryable GraphQL error: %s", first)
                await self._sleep(min(2**attempt, 30))
                continue
            if tolerate_missing and kinds <= MISSING_TYPES:
                log.debug("GraphQL NOT_FOUND (tolerated): %s", first)
                return result
            raise GraphQLError(f"GraphQL error: {first}", errors)

    def _with_budget(self, data: dict[str, Any], errors: list[dict[str, Any]]) -> QueryResult:
        limit = data.get("rateLimit") or {}
        cost = int(limit.get("cost") or 0)
        self.points_used += cost
        return QueryResult(
            data=data,
            cost=cost,
            remaining=limit.get("remaining"),
            reset_at=limit.get("resetAt"),
            errors=errors,
        )

    async def _wait_for_budget(self, result: QueryResult) -> None:
        """RATE_LIMITED: park every token until the stated reset, then retry."""
        reset = _epoch(result.reset_at)
        wait = max((reset or 0) - self._clock(), 60.0)
        for state in self.pool.states:
            state.remaining = 0
            state.reset_at = reset or (self._clock() + wait)
        log.warning("GraphQL point budget exhausted; sleeping %.0fs", wait)
        await self._sleep(wait)

    # ------------------------------------------------------------ pagination

    async def paginate(
        self,
        query: str,
        variables: Mapping[str, Any],
        path: Sequence[str],
        *,
        page_size: int = 100,
        after: str | None = None,
        max_pages: int = 0,
    ) -> AsyncIterator[Page]:
        """Walk a cursor-paginated connection at ``path``, shrinking pages on node limits.

        ``after`` resumes a previous crawl. Stop early by breaking out of the loop:
        the Page you were handed carries the cursor you should persist.
        """
        cursor = after
        size = max(1, min(100, page_size))
        pages = 0
        base = dict(variables)
        seen_cursors: set[str] = set()

        while True:
            try:
                result = await self.execute(query, {**base, "first": size, "after": cursor})
            except NodeLimitExceeded:
                if size <= 1:
                    raise
                size = max(1, size // 2)
                log.warning("Node limit hit; retrying with first=%d", size)
                continue

            connection = result.pluck(*path)
            if not isinstance(connection, Mapping):
                return  # repo missing / blocked: nothing to walk

            info = connection.get("pageInfo") or {}
            parent = result.pluck(*path[:-1]) if len(path) > 1 else result.data
            page = Page(
                connection=dict(connection),
                cost=result.cost,
                cursor=info.get("endCursor"),
                has_next=bool(info.get("hasNextPage")),
                parent=dict(parent) if isinstance(parent, Mapping) else {},
            )
            yield page

            pages += 1
            if not page.has_next or not page.cursor:
                return
            if max_pages and pages >= max_pages:
                return
            if page.cursor in seen_cursors:
                # A connection that keeps handing back a cursor we have already used
                # would paginate forever. Stop rather than spin.
                log.warning("Connection %s repeated cursor %s; stopping", ".".join(path), page.cursor)
                return
            seen_cursors.add(page.cursor)
            cursor = page.cursor


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return float(raw) + 1
        except ValueError:
            pass
    return 60.0


def _epoch(reset_at: str | None) -> float | None:
    """GraphQL reports resetAt as ISO-8601; the TokenPool wants epoch seconds."""
    if not reset_at:
        return None
    try:
        return datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
