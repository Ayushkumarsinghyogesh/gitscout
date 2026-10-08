"""Orchestration: ingest -> enrich -> score -> export, across all three backends."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from .config import Settings
from .enrich import Enricher, EnrichSummary
from .enrich_gql import EnrichGqlSummary, GqlEnricher
from .export import write_export
from .github_client import GitHubClient
from .graphql import GraphQLClient
from .ingest import IngestResult, ingest_kind, parse_repo
from .ingest_gql import EmailScope, GqlIngestResult, IngestTotals, ingest_repos_gql
from .models import ALL_KINDS, KINDS, Target
from .providers import load_providers, run_providers
from .scoring import rescore
from .storage import Store
from .targets import targets_from_repos
from .web import HostCheck, WebsiteScanner, is_public_host

log = logging.getLogger(__name__)


def parse_kinds(value: str | Sequence[str], allowed: Sequence[str] = ALL_KINDS) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else list(value)
    kinds = tuple(dict.fromkeys(k.strip().lower() for k in items if k.strip()))
    bad = [k for k in kinds if k not in allowed]
    if bad or not kinds:
        raise ValueError(f"kinds must be a comma list of {', '.join(allowed)} (got {value!r})")
    return kinds


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --------------------------------------------------------------------- REST path


@dataclass
class PipelineResult:
    ingest: list[IngestResult] = field(default_factory=list)
    enrich: EnrichSummary = field(default_factory=EnrichSummary)
    exported: int = 0


async def ingest_repos(
    client: GitHubClient,
    store: Store,
    repos: Sequence[str],
    kinds: Sequence[str],
    *,
    max_items: int = 0,
    fresh: bool = False,
) -> list[IngestResult]:
    unsupported = [k for k in kinds if k not in KINDS]
    if unsupported:
        raise ValueError(
            f"the REST backend does not support {unsupported}; "
            f"use --api graphql for {', '.join(sorted(set(ALL_KINDS) - set(KINDS)))}"
        )
    results: list[IngestResult] = []
    for raw in repos:
        repo = parse_repo(raw)
        results.extend(
            await asyncio.gather(
                *(
                    ingest_kind(client, store, repo, kind, max_items=max_items, fresh=fresh)
                    for kind in kinds
                )
            )
        )
    return results


async def run_pipeline(
    settings: Settings,
    repos: Sequence[str],
    kinds: Sequence[str] = KINDS,
    *,
    max_items: int = 0,
    fresh: bool = False,
    deep: bool = False,
    scan_websites: bool = False,
    out: str | Path | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
    host_check: HostCheck = is_public_host,
    progress: Callable[[int, int], None] | None = None,
) -> PipelineResult:
    """The original REST pipeline, unchanged. Kept as `--api rest`."""
    result = PipelineResult()
    canonical = [parse_repo(r) for r in repos]
    with Store(settings.db_path) as store:
        async with GitHubClient(
            settings.tokens, user_agent=settings.user_agent, transport=transport
        ) as client:
            result.ingest = await ingest_repos(
                client, store, canonical, kinds, max_items=max_items, fresh=fresh
            )
            async with Enricher(
                client,
                store,
                deep=deep,
                scan_websites=scan_websites,
                max_repos=settings.max_repos_for_commits,
                concurrency=settings.concurrency,
                http=http,
                host_check=host_check,
            ) as enricher:
                result.enrich = await enricher.run(progress=progress)
        if out:
            result.exported = write_export(
                store,
                out,
                repos=canonical,
                only_with_email=only_with_email,
                min_confidence=min_confidence,
            )[0]
    return result


# ------------------------------------------------------------------ GraphQL path


@dataclass
class ScoutResult:
    """Everything one scout run did, for the CLI and the audit log."""

    run_id: int | None = None
    ingest: IngestTotals = field(default_factory=IngestTotals)
    enrich: EnrichGqlSummary = field(default_factory=EnrichGqlSummary)
    provider_hits: int = 0
    provider_calls: int = 0
    scored: int = 0
    email_scope_missing: bool = False  # token lacked read:user; profile emails skipped
    exports: list[tuple[str, int, str]] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    status: str = "ok"
    error: str | None = None

    @property
    def points(self) -> int:
        return self.ingest.points + self.enrich.points

    @property
    def emails_new(self) -> int:
        return self.ingest.emails_new + self.enrich.emails_new + self.provider_hits

    @property
    def exported(self) -> int:
        return self.exports[0][1] if self.exports else 0


async def _run_providers_stage(
    settings: Settings,
    store: Store,
    result: ScoutResult,
    *,
    limit: int | None,
) -> None:
    """Optional paid lookups, only for people GitHub could not resolve."""
    providers = load_providers(settings)
    if not providers:
        return
    budget = settings.provider_budget or None
    logins = store.logins_without_email(limit=limit, column="provider_probed_at")
    if budget:
        logins = logins[:budget]
    if not logins:
        return

    profiles = store.profiles(logins)
    try:
        for login in logins:
            profile = profiles.get(login)
            if profile is None:
                continue
            result.provider_calls += 1
            found = await run_providers(providers, profile)
            if found:
                result.provider_hits += store.add_emails(login, found)
        store.mark_probed(logins, "provider_probed_at")
    finally:
        for provider in providers:
            await provider.aclose()


async def run_scout(
    settings: Settings,
    targets: Sequence[Target],
    *,
    max_items: int = 0,
    fresh: bool = False,
    incremental: bool = False,
    enrich: bool = True,
    enrich_limit: int | None = None,
    use_providers: bool = True,
    scan_websites: bool = False,
    outputs: Sequence[str | Path] = (),
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    new_only: bool = False,
    mode: str = "scout",
    transport: httpx.AsyncBaseTransport | None = None,
    http: httpx.AsyncClient | None = None,
    host_check: HostCheck = is_public_host,
    progress: Callable[[int, int], None] | None = None,
) -> ScoutResult:
    """The GraphQL pipeline: the one the scheduler runs.

    ``incremental=True`` is the cron mode -- only interactions newer than the last run
    are fetched. ``new_only=True`` then exports only the people found by *this* run,
    which is what you want feeding an outreach sequence.
    """
    result = ScoutResult()
    repo_list = [t.repo for t in targets]
    scope = EmailScope()  # shared across every repo+kind: warn once, not per target

    with Store(settings.db_path) as store:
        result.run_id = store.start_run(mode, ",".join(repo_list))
        try:
            async with GraphQLClient(
                settings.tokens, user_agent=settings.user_agent, transport=transport
            ) as client:
                # Grouped by kind-set so a target can opt out of noisy signals.
                for target in targets:
                    totals = await ingest_repos_gql(
                        client,
                        store,
                        [target.repo],
                        target.kinds,
                        max_items=max_items,
                        fresh=fresh,
                        incremental=incremental,
                        page_size=settings.page_size,
                        scope=scope,
                    )
                    result.ingest.results.extend(totals.results)

                if enrich:
                    scanner = (
                        WebsiteScanner(http, host_check=host_check) if scan_websites else None
                    )
                    try:
                        async with GqlEnricher(
                            client,
                            store,
                            batch_size=settings.commit_batch,
                            repos_per_user=settings.max_repos_for_commits,
                            commits_per_repo=settings.commits_per_repo,
                            scan_websites=scan_websites,
                            scanner=scanner,
                            website_concurrency=settings.concurrency,
                        ) as enricher:
                            result.enrich = await enricher.run(
                                limit=enrich_limit, progress=progress
                            )
                    finally:
                        if scanner is not None:
                            await scanner.aclose()

            if enrich and use_providers:
                await _run_providers_stage(settings, store, result, limit=enrich_limit)

            result.email_scope_missing = scope.downgraded
            result.scored = rescore(store)

            if outputs:
                new_since = result.started_at if new_only else None
                for path in outputs:
                    count, fmt = write_export(
                        store,
                        path,
                        repos=repo_list,
                        only_with_email=only_with_email,
                        min_confidence=min_confidence,
                        new_since=new_since,
                        order_by_score=True,
                    )
                    result.exports.append((str(path), count, fmt))
        except Exception as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            _finish(store, result)
            raise
        _finish(store, result)
    return result


def _finish(store: Store, result: ScoutResult) -> None:
    from .models import RunRecord

    store.finish_run(
        RunRecord(
            run_id=result.run_id,
            interactions_new=result.ingest.new,
            users_new=sum(r.profiles for r in result.ingest.results),
            emails_new=result.emails_new,
            points_used=result.points,
            status=result.status,
            error=result.error,
        )
    )


# -------------------------------------------------------------------- Apify path


async def run_apify(
    settings: Settings,
    repos: Sequence[str],
    kinds: Sequence[str] = ("stars",),
    *,
    outputs: Sequence[str | Path] = (),
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    http: httpx.AsyncClient | None = None,
) -> ScoutResult:
    """Fallback path. See the module docstring in [apify.py] for the trade-offs."""
    from .apify import ApifyClient, ingest_repos_apify

    result = ScoutResult()
    canonical = [parse_repo(r) for r in repos]
    with Store(settings.db_path) as store:
        result.run_id = store.start_run("apify", ",".join(canonical))
        try:
            async with ApifyClient(
                settings.apify_token or "", actor=settings.apify_actor, http=http
            ) as client:
                runs = await ingest_repos_apify(client, store, canonical, kinds)
            # Report Apify runs in the same shape as the GraphQL path, so the CLI and
            # the audit log do not need to know which backend produced them.
            for run in runs:
                result.ingest.results.append(
                    GqlIngestResult(
                        repo=run.repo,
                        kind=run.kind,
                        fetched=run.fetched,
                        new=run.new,
                        profiles=run.profiles,
                        emails_new=run.emails_new,
                        points=0,  # Apify bills in dollars, not GraphQL points
                        pages=1,
                        complete=True,
                        total_count=run.items,
                    )
                )
            result.scored = rescore(store)
            for path in outputs:
                count, fmt = write_export(
                    store,
                    path,
                    repos=canonical,
                    only_with_email=only_with_email,
                    min_confidence=min_confidence,
                    order_by_score=True,
                )
                result.exports.append((str(path), count, fmt))
        except Exception as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            _finish(store, result)
            raise
        _finish(store, result)
    return result


def targets_for(
    repos: Sequence[str],
    kinds: Sequence[str],
    targets_file: str | None = None,
) -> list[Target]:
    """Build the target list from a profile file, explicit repos, or both."""
    from .targets import load_targets

    out: list[Target] = []
    if targets_file:
        out.extend(load_targets(targets_file))
    if repos:
        out.extend(targets_from_repos(repos, kinds))
    if not out:
        raise ValueError("nothing to scout: pass a repo or --targets <profile>")
    seen: dict[str, Target] = {}
    for target in out:
        seen[target.repo] = target  # later entry wins
    return list(seen.values())
