"""Preflight checks: `gitscout doctor`.

The point of this module is honesty. GraphQL queries cannot be exercised without a
real token, and GitHub's schema does change, so rather than *assume* the documents in
[queries.py] still match the live API, doctor runs every one of them against a tiny
public repo and reports exactly what came back.

Run it first, after any schema change, and in CI. A green doctor means the pipeline's
API surface genuinely works against today's GitHub.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx

from .config import Settings
from .github_client import GitHubAuthError, GitHubClient
from .graphql import GraphQLClient, GraphQLError, MissingScopeError
from .queries import (
    DISCOVER,
    DISCOVER_PATH,
    KIND_QUERIES,
    PROBE_REPO,
    VIEWER,
    commit_email_query,
    kind_query,
)
from .storage import Store

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    @property
    def symbol(self) -> str:
        return {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}[self.status]


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> Check:
        check = Check(name, status, detail)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failed

    def lines(self) -> list[str]:
        width = max((len(c.name) for c in self.checks), default=0)
        return [f"[{c.symbol}] {c.name.ljust(width)}  {c.detail}".rstrip() for c in self.checks]


# ----------------------------------------------------------------- local checks


def check_database(settings: Settings, report: Report) -> None:
    path = Path(settings.db_path)
    try:
        with Store(settings.db_path) as store:
            stats = store.stats()
        where = "in memory" if settings.db_path == ":memory:" else str(path.resolve())
        report.add(
            "database",
            OK,
            f"{where} - {stats['interactions']} interactions, "
            f"{stats['unique_users']} users, {stats['users_with_email']} with email",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("database", FAIL, f"cannot open {settings.db_path}: {exc}")


def check_config(settings: Settings, report: Report) -> None:
    if settings.tokens:
        report.add(
            "github token",
            OK,
            f"{len(settings.tokens)} token(s) configured "
            f"= {len(settings.tokens) * 5000} GraphQL points/hour",
        )
    else:
        report.add(
            "github token",
            FAIL,
            "GITHUB_TOKENS is empty. GitHub now returns 401 on anonymous API calls, "
            "so nothing will work. Create one at https://github.com/settings/tokens",
        )

    providers = settings.providers_enabled
    report.add(
        "email providers",
        OK,
        ", ".join(providers) if providers else "none enabled (GitHub-only; this is the default)",
    )
    report.add(
        "apify fallback",
        OK,
        f"configured, actor={settings.apify_actor}" if settings.apify_token else "not configured",
    )


# ------------------------------------------------------------------ live checks


async def check_rest(settings: Settings, report: Report, **kw: Any) -> None:
    async with GitHubClient(settings.tokens, user_agent=settings.user_agent, **kw) as client:
        try:
            rows = await client.rate_limits()
        except GitHubAuthError as exc:
            report.add("rest api", FAIL, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            report.add("rest api", FAIL, f"{type(exc).__name__}: {exc}")
            return
    bad = [r for r in rows if r.get("error")]
    if bad:
        report.add("rest api", FAIL, f"{len(bad)} token(s) rejected: {bad[0].get('error')}")
        return
    remaining = sum(int(r.get("remaining") or 0) for r in rows)
    report.add("rest api", OK, f"reachable, {remaining} REST requests remaining this hour")


async def check_graphql(settings: Settings, report: Report, **kw: Any) -> None:
    """Run every production query against a tiny public repo and report the result."""
    owner, name = PROBE_REPO
    async with GraphQLClient(settings.tokens, user_agent=settings.user_agent, **kw) as client:
        try:
            result = await client.execute(VIEWER, tolerate_missing=False)
        except GitHubAuthError as exc:
            report.add("graphql auth", FAIL, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            report.add("graphql auth", FAIL, f"{type(exc).__name__}: {exc}")
            return

        login = (result.pluck("viewer", "login")) or "?"
        report.add(
            "graphql auth",
            OK,
            f"authenticated as {login}, {result.remaining} points remaining",
        )

        scope_warned = False
        for kind in KIND_QUERIES:
            query, path = kind_query(kind, include_email=not scope_warned)
            try:
                res = await client.execute(
                    query,
                    {"owner": owner, "name": name, "first": 1, "after": None},
                    tolerate_missing=False,
                )
            except MissingScopeError:
                # Not a schema problem: the token simply cannot read User.email.
                # Report it once, then check the remaining queries without the field.
                if not scope_warned:
                    scope_warned = True
                    report.add(
                        "scope: read:user",
                        WARN,
                        "this token cannot read profile emails. Everything else works, "
                        "and commit emails are unaffected. Tick 'read:user' at "
                        "https://github.com/settings/tokens to enable them",
                    )
                query, path = kind_query(kind, include_email=False)
                try:
                    res = await client.execute(
                        query,
                        {"owner": owner, "name": name, "first": 1, "after": None},
                        tolerate_missing=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    report.add(f"query: {kind}", FAIL, f"{type(exc).__name__}: {exc}")
                    continue
            except GraphQLError as exc:
                report.add(f"query: {kind}", FAIL, f"schema mismatch - {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                report.add(f"query: {kind}", FAIL, f"{type(exc).__name__}: {exc}")
                continue
            connection = res.pluck(*path)
            if connection is None:
                report.add(f"query: {kind}", FAIL, f"no {'.'.join(path)} in the response")
                continue
            total = connection.get("totalCount")
            report.add(
                f"query: {kind}",
                OK,
                f"{owner}/{name} reports totalCount={total}, cost={res.cost}",
            )

        # commit probe (the batched one, with a single alias)
        query, variables = commit_email_query([login if login != "?" else owner])
        variables.update({"repos": 1, "commits": 1})
        try:
            res = await client.execute(query, variables, tolerate_missing=False)
            probe = res.data.get("u0")
            report.add(
                "query: commit emails",
                OK if probe is not None else WARN,
                f"cost={res.cost}" if probe is not None else "returned no user node",
            )
        except Exception as exc:  # noqa: BLE001
            report.add("query: commit emails", FAIL, f"{type(exc).__name__}: {exc}")

        # discovery search
        try:
            res = await client.execute(
                DISCOVER,
                {"q": "topic:cloud-security stars:>=100 archived:false", "first": 1, "after": None},
                tolerate_missing=False,
            )
            count = (res.pluck(*DISCOVER_PATH) or {}).get("repositoryCount")
            report.add("query: discover", OK, f"search works, repositoryCount={count}")
        except Exception as exc:  # noqa: BLE001
            report.add("query: discover", FAIL, f"{type(exc).__name__}: {exc}")

        report.add(
            "graphql budget",
            OK,
            f"this check cost {client.points_used} points over {client.queries_sent} queries"
            + (" (some retried without the email field)" if scope_warned else ""),
        )


async def check_apify(settings: Settings, report: Report, http: httpx.AsyncClient | None = None) -> None:
    if not settings.apify_token:
        return
    from .apify import ApifyError

    owns = http is None
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(
            f"https://api.apify.com/v2/acts/{settings.apify_actor.replace('/', '~')}",
            params={"token": settings.apify_token},
        )
        if resp.status_code in (401, 403):
            report.add("apify api", FAIL, "token rejected")
        elif resp.status_code == 404:
            report.add("apify api", FAIL, f"actor not found: {settings.apify_actor}")
        elif resp.status_code >= 400:
            report.add("apify api", WARN, f"HTTP {resp.status_code}")
        else:
            report.add("apify api", OK, f"actor {settings.apify_actor} reachable")
    except (httpx.HTTPError, ApifyError) as exc:
        report.add("apify api", WARN, f"{type(exc).__name__}: {exc}")
    finally:
        if owns:
            await client.aclose()


async def run_doctor(
    settings: Settings,
    *,
    live: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
) -> Report:
    """Run every check. `live=False` skips anything that touches the network."""
    report = Report()
    check_config(settings, report)
    check_database(settings, report)

    if not live:
        report.add("live checks", WARN, "skipped (--offline)")
        return report
    if not settings.tokens:
        report.add("live checks", WARN, "skipped: no token to call the API with")
        return report

    kw: dict[str, Any] = {"transport": transport} if transport is not None else {}
    await check_rest(settings, report, **kw)
    await check_graphql(settings, report, **kw)
    await check_apify(settings, report, http=http)
    return report


def targets_report(paths: Sequence[str | Path]) -> Report:
    """Validate target profiles without touching the network."""
    from .targets import load_targets

    report = Report()
    for path in paths:
        try:
            targets = load_targets(path)
        except Exception as exc:  # noqa: BLE001
            report.add(f"targets: {path}", FAIL, f"{type(exc).__name__}: {exc}")
            continue
        kinds = sorted({k for t in targets for k in t.kinds})
        report.add(
            f"targets: {Path(str(path)).name}",
            OK,
            f"{len(targets)} repos, kinds={','.join(kinds)}",
        )
    return report
