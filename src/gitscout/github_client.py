"""Async GitHub REST client: token rotation, rate-limit handling, retries, pagination."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.github.com"
SleepFn = Callable[[float], Awaitable[None]]


class GitHubError(RuntimeError):
    """Unrecoverable GitHub API problem."""


class GitHubAuthError(GitHubError):
    """401: the token is invalid or revoked."""


@dataclass
class TokenState:
    token: str | None
    remaining: int | None = None  # unknown until the first response
    reset_at: float = 0.0  # epoch seconds

    @property
    def label(self) -> str:
        if not self.token:
            return "anonymous"
        return f"{self.token[:4]}...{self.token[-4:]}" if len(self.token) > 10 else "token"


class TokenPool:
    """Round-robins tokens; sleeps until the earliest reset when all are exhausted."""

    RESERVE = 1  # keep one request in hand per token

    def __init__(
        self,
        tokens: Iterable[str],
        *,
        clock: Callable[[], float] = time.time,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self.states = [TokenState(t) for t in tokens] or [TokenState(None)]
        self._clock = clock
        self._sleep = sleep
        self._rr = 0

    async def acquire(self) -> TokenState:
        while True:
            now = self._clock()
            usable = [
                s
                for s in self.states
                if s.remaining is None or s.remaining > self.RESERVE or s.reset_at <= now
            ]
            if usable:
                state = usable[self._rr % len(usable)]
                self._rr += 1
                if state.remaining is not None and state.remaining <= self.RESERVE:
                    state.remaining = None  # window has reset
                return state
            wait = min(s.reset_at for s in self.states) - now + 1
            log.warning("All tokens rate-limited; sleeping %.0fs until reset", wait)
            await self._sleep(max(wait, 1.0))

    @staticmethod
    def update(state: TokenState, headers: Mapping[str, str]) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                state.remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                state.reset_at = float(reset)
            except ValueError:
                pass


class GitHubClient:
    def __init__(
        self,
        tokens: Iterable[str] = (),
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = "gitscout/0.1",
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 5,
        timeout: float = 30.0,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep
        self.pool = TokenPool(tokens, clock=clock, sleep=sleep)
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": user_agent,
            },
        )

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def request(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response | None:
        """GET with retries. Returns None for 404/409/410/451 (missing / empty / blocked)."""
        if url.startswith(("http://", "https://")) and not url.startswith(self._base_url + "/"):
            # never send credentials anywhere except the GitHub API host
            raise GitHubError(f"Refusing to call non-GitHub URL: {url}")

        attempt = 0
        primary_waits = 0
        while True:
            state = await self.pool.acquire()
            req_headers: dict[str, str] = dict(headers or {})
            if state.token:
                req_headers["Authorization"] = f"Bearer {state.token}"
            try:
                resp = await self._http.get(url, params=params, headers=req_headers)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubError(f"Network error calling {url}: {exc}") from exc
                await self._sleep(min(2**attempt, 30))
                continue

            self.pool.update(state, resp.headers)
            status = resp.status_code

            if status == 200:
                return resp
            if status in (404, 409, 410, 451):
                return None
            if status == 401:
                raise GitHubAuthError(f"Bad credentials for token {state.label}")
            if status in (403, 429):
                wait = self._rate_limit_wait(resp)
                if wait is None:
                    raise GitHubError(f"403 Forbidden for {url}: {resp.text[:200]}")
                if wait == 0:  # primary limit: pool.acquire() will wait for the reset
                    primary_waits += 1
                    if primary_waits > 10:
                        raise GitHubError("Rate limit never recovered")
                    continue
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubError(f"Secondary rate limit not clearing for {url}")
                log.warning("Secondary rate limit; sleeping %.0fs", wait)
                await self._sleep(wait)
                continue
            if status >= 500:
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubError(f"HTTP {status} from {url} after retries")
                await self._sleep(min(2**attempt, 30))
                continue
            raise GitHubError(f"HTTP {status} for {url}: {resp.text[:200]}")

    @staticmethod
    def _rate_limit_wait(resp: httpx.Response) -> float | None:
        """0 = primary limit (pool handles it), >0 = seconds to back off, None = not a limit."""
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return 0.0
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 1
            except ValueError:
                return 60.0
        if resp.status_code == 429 or "rate limit" in resp.text.lower():
            return 60.0
        return None

    async def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any | None:
        resp = await self.request(url, params, headers)
        return None if resp is None else resp.json()

    async def pages(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        start_url: str | None = None,
    ) -> AsyncIterator[tuple[list[Any], str | None]]:
        """Yield (items, next_url) per page following Link headers. start_url resumes a crawl."""
        current: str | None = start_url or url
        current_params = None if start_url else params
        while current:
            resp = await self.request(current, current_params, headers)
            if resp is None:
                return
            data = resp.json()
            if not isinstance(data, list):
                raise GitHubError(f"Expected a list from {current}, got {type(data).__name__}")
            next_url = resp.links.get("next", {}).get("url")
            yield data, next_url
            current, current_params = next_url, None

    async def rate_limits(self) -> list[dict[str, Any]]:
        """Core rate-limit status for each configured token (does not consume quota)."""
        out: list[dict[str, Any]] = []
        for state in self.pool.states:
            hdrs = {"Authorization": f"Bearer {state.token}"} if state.token else {}
            resp = await self._http.get("/rate_limit", headers=hdrs)
            if resp.status_code == 401:
                out.append({"token": state.label, "error": "invalid token"})
                continue
            core = resp.json().get("resources", {}).get("core", {})
            out.append(
                {
                    "token": state.label,
                    "limit": core.get("limit"),
                    "remaining": core.get("remaining"),
                    "reset_epoch": core.get("reset"),
                }
            )
        return out
