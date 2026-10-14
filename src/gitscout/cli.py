"""Command line interface:  gitscout scout --targets cloud-security -o leads.csv"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Optional, Sequence

import typer

from .config import Settings, load_settings
from .enrich import Enricher
from .enrich_gql import GqlEnricher
from .export import FORMATS, write_export, write_thread_comments, write_threads
from .github_client import GitHubClient, GitHubError
from .graphql import GraphQLClient
from .threads_gql import (
    DEFAULT_COMMENTS,
    DEFAULT_PAGE_SIZE,
    ThreadTotals,
    fetch_comment_tails,
    fetch_repo_info,
    harvest_repos,
    resolve_participants,
    resolve_social,
)
from .models import (
    ALL_KINDS,
    DEFAULT_KINDS,
    KINDS,
    RESTRICTED_KINDS,
    THREAD_KINDS,
    THREAD_ROLES,
)
from .pipeline import (
    ScoutResult,
    ingest_repos,
    parse_kinds,
    run_apify,
    run_pipeline,
    run_scout,
    targets_for,
)
from .scoring import rescore
from .lock import RunLocked, run_lock
from .storage import Store
from .supabase import (
    SYNC_KEY,
    TARGETS,
    PostgresWriter,
    SupabaseClient,
    SupabaseError,
    schema_sql,
)
from .supabase import push as supabase_push_rows
from .targets import available_profiles, discover, load_targets, to_toml

app = typer.Typer(
    help=(
        "Find the people who star, fork, file issues or open PRs on GitHub repos, "
        "and discover their public emails. GraphQL-first, cron-ready."
    ),
    no_args_is_help=True,
    add_completion=False,
)

RepoArgs = Annotated[Optional[list[str]], typer.Argument(help="owner/name or GitHub URL")]
KindsOpt = Annotated[
    Optional[str],
    typer.Option(
        "--kinds",
        "-k",
        help=(
            f"Comma list of: {', '.join(ALL_KINDS)}. "
            f"Default: {','.join(DEFAULT_KINDS)} "
            f"({','.join(sorted(RESTRICTED_KINDS))} comes from the events API: recent only)"
        ),
    ),
]
MaxOpt = Annotated[int, typer.Option("--max", help="Soft cap per repo+kind per run (0 = unlimited)")]
FreshOpt = Annotated[bool, typer.Option("--fresh", help="Ignore saved cursors and re-crawl")]
DeepOpt = Annotated[bool, typer.Option("--deep", help="Run every email source, not just until first hit")]
WebOpt = Annotated[bool, typer.Option("--scan-websites", help="Also scan the site linked from profiles")]
OnlyEmailOpt = Annotated[bool, typer.Option("--only-with-email", help="Export only rows with an email")]
MinConfOpt = Annotated[float, typer.Option("--min-confidence", help="Drop emails scoring below this (0-1)")]
TargetsOpt = Annotated[Optional[str], typer.Option("--targets", "-t", help="Target profile (name or path)")]
OutOpt = Annotated[Optional[list[Path]], typer.Option("--out", "-o", help="Output file (repeatable; .csv or .jsonl)")]
CommentsOpt = Annotated[int, typer.Option("--comments", help=f"Comments stored per thread before it is marked truncated (default {DEFAULT_COMMENTS})")]
SectionsOpt = Annotated[Optional[str], typer.Option("--sections", help=f"Comma list of: {', '.join(THREAD_KINDS)}. Default: all three")]
TailsOpt = Annotated[int, typer.Option("--tails", help="Also fetch the NEWEST comments of up to N capped threads (1 query each)")]


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj


def _kinds(
    value: str | None,
    allowed: Sequence[str] = ALL_KINDS,
    default: Sequence[str] = DEFAULT_KINDS,
) -> tuple[str, ...]:
    """Resolve -k. None means "the caller did not say", so use the default set."""
    if value is None:
        return tuple(k for k in default if k in allowed)
    try:
        selected = parse_kinds(value, allowed)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    restricted = [k for k in selected if k in RESTRICTED_KINDS]
    if restricted:
        typer.secho(
            f"note: {', '.join(restricted)} is collected from the events API, because "
            "GitHub restricted the stargazer list to repo admins on 2026-06-30. "
            "That window holds only the last ~300 repo events, so you get recent stars, "
            "not the full history -- run it on a schedule to keep up with new ones.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    return selected


def _progress(done: int, total: int) -> None:
    if total and (done >= total or done % 50 == 0):
        typer.echo(f"  enriched {done}/{total}", err=True)


def _require_token(settings: Settings) -> None:
    """GitHub now 401s anonymous API calls, so this is fatal rather than a warning."""
    if not settings.tokens:
        typer.secho(
            "No GITHUB_TOKENS set.\n"
            "GitHub returns 401 for anonymous API requests, so this cannot run without one.\n"
            "Create a token (no scopes needed for public data) at "
            "https://github.com/settings/tokens and put it in .env as GITHUB_TOKENS=ghp_...",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


def _run(coro):
    try:
        return asyncio.run(coro)
    except GitHubError as exc:
        typer.secho(f"GitHub error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _report_threads(totals: ThreadTotals) -> None:
    for r in totals.results:
        if r.skipped and not r.fetched:
            continue
        total = f"/{r.total_count}" if r.total_count else ""
        typer.echo(
            f"{r.repo:34s} {r.kind:12s} fetched={r.fetched:<6d}{total:<9s} "
            f"new={r.new:<6d} +{r.comments_new} comments  {r.status}"
            + (f"  ({r.truncated} capped)" if r.truncated else "")
        )
    if totals.results:
        typer.echo(
            f"cost:   {totals.points} GraphQL points"
            + (f", {totals.tails} tail(s) fetched" if totals.tails else "")
        )


def _report_scout(result: ScoutResult) -> None:
    for r in result.ingest.results:
        total = f"/{r.total_count}" if r.total_count else ""
        typer.echo(
            f"{r.repo:40s} {r.kind:7s} fetched={r.fetched:<6d}{total:<9s} "
            f"new={r.new:<6d} {r.status}"
        )
    _report_threads(result.threads)
    if result.participants_resolved:
        typer.echo(
            f"participants: {result.participants_resolved} comment-only people looked up, "
            f"+{result.participant_emails} emails"
        )
    e = result.enrich
    typer.echo(
        f"\nemails: +{result.ingest.emails_new} from profiles, "
        f"+{e.emails_new} from commits/websites"
        + (f", +{result.provider_hits} from providers" if result.provider_calls else "")
    )
    thread_cost = f" + {result.threads.points} threads" if result.threads.results else ""
    typer.echo(
        f"cost:   {result.points} GraphQL points "
        f"({result.ingest.points} ingest{thread_cost} + {e.points} enrich, "
        f"{e.queries} batched queries)"
    )
    if e.failed:
        typer.secho(f"warning: {e.failed} user(s) failed enrichment", fg=typer.colors.YELLOW)
    if result.email_scope_missing:
        typer.secho(
            "warning: profile emails were skipped -- this token lacks the 'read:user' "
            "scope. Add it at https://github.com/settings/tokens, then re-run with "
            "--fresh to pick them up.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    for path, count, fmt in result.exports:
        typer.echo(f"wrote:  {count} rows -> {path} ({fmt})")


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Optional[str], typer.Option("--db", help="SQLite file (env GITSCOUT_DB)")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ctx.obj = load_settings(db)


# --------------------------------------------------------------- primary command


@app.command()
def scout(
    ctx: typer.Context,
    repos: RepoArgs = None,
    targets: TargetsOpt = None,
    kinds: KindsOpt = None,
    max_items: MaxOpt = 0,
    fresh: FreshOpt = False,
    incremental: Annotated[bool, typer.Option("--incremental", help="Only fetch what is new since the last run")] = False,
    no_enrich: Annotated[bool, typer.Option("--no-enrich", help="Skip email discovery")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Max users to enrich this run (0 = all)")] = 0,
    scan_websites: WebOpt = False,
    no_providers: Annotated[bool, typer.Option("--no-providers", help="Skip paid providers even if keys are set")] = False,
    out: OutOpt = None,
    new_only: Annotated[bool, typer.Option("--new-only", help="Export only people found by THIS run")] = False,
    only_with_email: OnlyEmailOpt = False,
    min_confidence: MinConfOpt = 0.0,
    with_threads: Annotated[bool, typer.Option("--with-threads", help="Also collect full issue/PR/discussion records + comment threads (costs more; see `gitscout threads`)")] = False,
    sections: SectionsOpt = None,
    thread_comments: CommentsOpt = DEFAULT_COMMENTS,
    thread_tails: TailsOpt = 0,
    with_social: Annotated[bool, typer.Option("--with-social", help="Also fetch the LinkedIn link and public repo count people publish (~1 point / 25 people)")] = False,
    push: Annotated[bool, typer.Option("--push", help="Then register the repos, push to Supabase and refresh the outreach dataset. Holds a run lock.")] = False,
) -> None:
    """GraphQL pipeline: ingest -> email discovery -> score -> export. The main command.

    The scheduled run is one invocation of this:

        gitscout scout -t cloud-security --incremental --with-threads --with-social --push

    Adding a repository to the profile is all it takes to bring it into the next run.
    """
    settings = _settings(ctx)
    _require_token(settings)
    if push and settings.supabase_transport != "postgres":
        raise typer.BadParameter(
            "--push needs SUPABASE_DB_URL (the gitscout_writer login); see "
            "docs/ingestion-and-scheduling.md"
        )
    target_list = _run_sync_targets(repos or [], _kinds(kinds), targets, kinds is not None)

    lock = run_lock(settings.db_path) if push else nullcontext()
    try:
        with lock:
            _scout_once(
                settings, target_list,
                max_items=max_items, fresh=fresh, incremental=incremental,
                no_enrich=no_enrich, limit=limit, scan_websites=scan_websites,
                no_providers=no_providers, out=out, new_only=new_only,
                only_with_email=only_with_email, min_confidence=min_confidence,
                with_threads=with_threads, sections=sections,
                thread_comments=thread_comments, thread_tails=thread_tails,
                with_social=with_social, push=push,
            )
    except RunLocked as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(3) from exc


def _scout_once(
    settings: Settings,
    target_list,
    *,
    max_items, fresh, incremental, no_enrich, limit, scan_websites, no_providers,
    out, new_only, only_with_email, min_confidence, with_threads, sections,
    thread_comments, thread_tails, with_social, push,
) -> None:
    result = _run(
        run_scout(
            settings,
            target_list,
            max_items=max_items,
            fresh=fresh,
            incremental=incremental,
            enrich=not no_enrich,
            enrich_limit=limit or None,
            use_providers=not no_providers,
            scan_websites=scan_websites,
            outputs=out or [],
            only_with_email=only_with_email,
            min_confidence=min_confidence,
            new_only=new_only,
            with_threads=with_threads,
            thread_kinds=_kinds(sections, THREAD_KINDS) if sections else THREAD_KINDS,
            thread_comments=thread_comments,
            thread_tails=thread_tails,
            progress=_progress,
        )
    )
    _report_scout(result)

    if not (with_social or push):
        return

    async def extras():
        with Store(settings.db_path) as store:
            async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
                social = await resolve_social(client, store) if with_social else None
                # The registry carries each repository's stable id; views need it.
                repos = [await fetch_repo_info(client, store, t.repo) for t in target_list] if push else []
                return social, repos, client.points_used

    social, repos, points = _run(extras())
    if social:
        typer.echo(f"social:  {social[0]} looked up, {social[1]} LinkedIn links")
    if push:
        typer.echo(f"registry: {', '.join(r['repo'] for r in repos if r)} ({points} points incl. social)")
        with Store(settings.db_path) as store:
            try:
                pushed = _run(supabase_push_rows(settings, store))
            except SupabaseError as exc:
                typer.secho(f"Supabase push failed: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1) from exc
        typer.echo(
            f"pushed:  {pushed.rows} rows "
            + ", ".join(f"{k}={v}" for k, v in pushed.counts.items() if v)
            + f" in {pushed.seconds}s"
        )
        _report_refresh(pushed)


def _run_sync_targets(
    repos: list[str], kinds: tuple[str, ...], targets_file: str | None, kinds_given: bool = False
):
    try:
        return targets_for(repos, kinds, targets_file, kinds_given=kinds_given)
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def watch(
    ctx: typer.Context,
    repos: RepoArgs = None,
    targets: TargetsOpt = None,
    kinds: KindsOpt = None,
    every: Annotated[str, typer.Option("--every", help="Interval: 30s, 15m, 6h, 1d")] = "6h",
    out: OutOpt = None,
    runs: Annotated[int, typer.Option("--runs", help="Stop after N runs (0 = forever)")] = 0,
    limit: Annotated[int, typer.Option("--limit", help="Max users to enrich per run")] = 0,
    scan_websites: WebOpt = False,
    all_rows: Annotated[bool, typer.Option("--all-rows", help="Export everything, not just this run's finds")] = False,
    only_with_email: OnlyEmailOpt = False,
    min_confidence: MinConfOpt = 0.0,
) -> None:
    """Run an incremental scout on a schedule, in this process (Ctrl-C to stop)."""
    from .scheduler import install_signal_handlers, watch as watch_loop

    settings = _settings(ctx)
    _require_token(settings)
    target_list = _run_sync_targets(repos or [], _kinds(kinds), targets, kinds is not None)

    async def go():
        stop = asyncio.Event()
        install_signal_handlers(stop)
        return await watch_loop(
            settings,
            target_list,
            every=every,
            max_runs=runs,
            outputs=out or [],
            new_only=not all_rows,
            only_with_email=only_with_email,
            min_confidence=min_confidence,
            enrich_limit=limit or None,
            scan_websites=scan_websites,
            stop=stop,
        )

    try:
        state = _run(go())
    except KeyboardInterrupt:
        typer.echo("\nstopped", err=True)
        raise typer.Exit(0) from None
    typer.echo(
        f"{state.runs} run(s), {state.failures} failure(s), "
        f"+{state.interactions_new} interactions, +{state.emails_new} emails, "
        f"{state.points} points"
    )
    if state.last_error:
        typer.secho(f"last error: {state.last_error}", fg=typer.colors.YELLOW, err=True)


@app.command()
def doctor(
    ctx: typer.Context,
    offline: Annotated[bool, typer.Option("--offline", help="Skip every network check")] = False,
    targets: TargetsOpt = None,
) -> None:
    """Validate the token, the database, and every GraphQL query against live GitHub."""
    from .doctor import run_doctor, targets_report

    settings = _settings(ctx)
    report = _run(run_doctor(settings, live=not offline))
    if targets:
        report.checks.extend(targets_report([targets]).checks)
    for line in report.lines():
        colour = (
            typer.colors.GREEN
            if line.startswith("[PASS]")
            else typer.colors.YELLOW
            if line.startswith("[WARN]")
            else typer.colors.RED
        )
        typer.secho(line, fg=colour)
    if report.failed:
        typer.secho(f"\n{len(report.failed)} check(s) failed.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho("\nAll checks passed.", fg=typer.colors.GREEN)


# ------------------------------------------------------------------ sub-commands


@app.command("discover")
def discover_cmd(
    ctx: typer.Context,
    topic: Annotated[Optional[str], typer.Option("--topic", help="GitHub topic, e.g. cloud-security")] = None,
    text: Annotated[Optional[str], typer.Option("--text", help="Free-text search terms")] = None,
    language: Annotated[Optional[str], typer.Option("--language")] = None,
    min_stars: Annotated[int, typer.Option("--min-stars")] = 500,
    limit: Annotated[int, typer.Option("--limit")] = 30,
    save: Annotated[Optional[Path], typer.Option("--save", help="Write the results as a target profile")] = None,
) -> None:
    """Find more repos worth watching, via the GitHub search API."""
    settings = _settings(ctx)
    _require_token(settings)

    async def go():
        async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
            return await discover(
                client,
                topic=topic,
                text=text,
                language=language,
                min_stars=min_stars,
                limit=limit,
            )

    rows = _run(go())
    for row in rows:
        typer.echo(
            f"{row['repo']:45s} {str(row['stars']):>7s} stars  "
            f"{(row['language'] or '-'):12s} {(row['description'] or '')[:60]}"
        )
    if save:
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(to_toml(rows, name=topic or text or "discovered"), encoding="utf-8")
        typer.echo(f"\nWrote {len(rows)} targets -> {save}")


@app.command()
def targets_list(ctx: typer.Context, name: TargetsOpt = None) -> None:
    """Show the shipped target profiles, or the contents of one."""
    if not name:
        profiles = available_profiles()
        if not profiles:
            typer.echo("No target profiles found.")
            return
        for path in profiles:
            typer.echo(f"{path.stem:20s} {path}")
        return
    try:
        for target in load_targets(name):
            kinds = ",".join(target.kinds)
            typer.echo(f"{target.repo:45s} {kinds:24s} {target.note or ''}")
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def ingest(
    ctx: typer.Context,
    repos: Annotated[list[str], typer.Argument(help="owner/name or GitHub URL")],
    kinds: KindsOpt = None,
    max_items: MaxOpt = 0,
    fresh: FreshOpt = False,
    api: Annotated[str, typer.Option("--api", help="graphql or rest")] = "graphql",
    incremental: Annotated[bool, typer.Option("--incremental")] = False,
) -> None:
    """Stage 1 only: collect stargazers / forkers / issue and PR authors."""
    settings = _settings(ctx)
    _require_token(settings)

    if api == "rest":
        selected = _kinds(kinds, KINDS)

        async def go_rest():
            with Store(settings.db_path) as store:
                async with GitHubClient(settings.tokens, user_agent=settings.user_agent) as client:
                    return await ingest_repos(
                        client, store, repos, selected, max_items=max_items, fresh=fresh
                    )

        for r in _run(go_rest()):
            status = "skipped (done)" if r.skipped else ("complete" if r.complete else "partial")
            typer.echo(f"{r.repo:40s} {r.kind:7s} fetched={r.fetched:<6d} new={r.new:<6d} {status}")
        return

    if api != "graphql":
        raise typer.BadParameter("--api must be graphql or rest")

    result = _run(
        run_scout(
            settings,
            _run_sync_targets(repos, _kinds(kinds), None),
            max_items=max_items,
            fresh=fresh,
            incremental=incremental,
            enrich=False,
        )
    )
    _report_scout(result)


@app.command()
def enrich(
    ctx: typer.Context,
    deep: DeepOpt = False,
    scan_websites: WebOpt = False,
    limit: Annotated[int, typer.Option("--limit", help="Max users this run (0 = all)")] = 0,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-process already enriched users")] = False,
    api: Annotated[str, typer.Option("--api", help="graphql or rest")] = "graphql",
) -> None:
    """Email discovery for everyone collected so far."""
    settings = _settings(ctx)
    _require_token(settings)

    if api == "rest":
        async def go_rest():
            with Store(settings.db_path) as store:
                async with GitHubClient(settings.tokens, user_agent=settings.user_agent) as client:
                    async with Enricher(
                        client,
                        store,
                        deep=deep,
                        scan_websites=scan_websites,
                        max_repos=settings.max_repos_for_commits,
                        concurrency=settings.concurrency,
                    ) as enricher:
                        return await enricher.run(
                            limit=limit or None, refresh=refresh, progress=_progress
                        )

        s = _run(go_rest())
        typer.echo(f"processed={s.processed} with_email={s.with_email} failed={s.failed}")
        return

    if api != "graphql":
        raise typer.BadParameter("--api must be graphql or rest")

    async def go():
        with Store(settings.db_path) as store:
            async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
                async with GqlEnricher(
                    client,
                    store,
                    batch_size=settings.commit_batch,
                    repos_per_user=settings.max_repos_for_commits,
                    commits_per_repo=settings.commits_per_repo,
                    scan_websites=scan_websites,
                    website_concurrency=settings.concurrency,
                ) as enricher:
                    summary = await enricher.run(
                        limit=limit or None, refresh=refresh, progress=_progress
                    )
            rescore(store)
            return summary

    s = _run(go())
    typer.echo(
        f"processed={s.processed} with_email={s.with_email} emails_new={s.emails_new} "
        f"queries={s.queries} points={s.points} failed={s.failed}"
    )


@app.command("apify")
def apify_cmd(
    ctx: typer.Context,
    repos: Annotated[list[str], typer.Argument(help="owner/name or GitHub URL")],
    kinds: KindsOpt = None,
    out: OutOpt = None,
    only_with_email: OnlyEmailOpt = False,
) -> None:
    """Fallback: scrape via an Apify actor instead of the GitHub API (costs money)."""
    settings = _settings(ctx)
    if not settings.apify_token:
        typer.secho(
            "APIFY_TOKEN is not set. Add it to .env, or use `gitscout scout` "
            "with a GitHub token instead (free, and better).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    typer.secho(
        "Note: Apify actors bill per result, re-scrape everything each run (no "
        "incremental mode), and give no provenance for the addresses they return.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    result = _run(
        run_apify(
            settings,
            repos,
            _kinds(kinds),
            outputs=out or [],
            only_with_email=only_with_email,
        )
    )
    _report_scout(result)


@app.command("threads")
def threads_cmd(
    ctx: typer.Context,
    repos: RepoArgs = None,
    targets: TargetsOpt = None,
    sections: SectionsOpt = None,
    max_items: MaxOpt = 0,
    comments: CommentsOpt = DEFAULT_COMMENTS,
    tails: TailsOpt = 0,
    page_size: Annotated[int, typer.Option("--page-size", help=f"Threads per query (default {DEFAULT_PAGE_SIZE}; each one drags a body and a thread behind it)")] = DEFAULT_PAGE_SIZE,
    incremental: Annotated[bool, typer.Option("--incremental", help="Only threads updated since the last run (UPDATED_AT order, so old threads with new comments are included)")] = False,
    fresh: FreshOpt = False,
    export_only: Annotated[bool, typer.Option("--export-only", help="Write what is already stored; make no API calls")] = False,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="One row per (person, thread), comments included: .csv or .jsonl")] = None,
    comments_out: Annotated[Optional[Path], typer.Option("--comments-out", help="One row per comment/review/reply, in thread order")] = None,
    role: Annotated[Optional[str], typer.Option("--role", help=f"Limit the export to one of: {', '.join(THREAD_ROLES)}")] = None,
    only_with_email: OnlyEmailOpt = False,
    min_confidence: MinConfOpt = 0.0,
    body_chars: Annotated[int, typer.Option("--body-chars", help="Truncate bodies in CSV output (0 = keep in full)")] = 0,
    comment_chars: Annotated[int, typer.Option("--comment-chars", help="Truncate each comment in the CSV thread cell (0 = keep in full)")] = 0,
) -> None:
    """Full issue / PR / discussion records for the people you collected.

    The interaction crawl records *that* someone files issues on a repo. This records
    *which* ones, what they wrote, and every comment, review and reply on the thread,
    into the `threads` and `thread_comments` tables. It is a separate stage because a
    page here costs roughly an order of magnitude more than the interaction crawl --
    run `gitscout doctor` to see the real per-page cost before sizing a backfill.
    """
    settings = _settings(ctx)
    if role and role not in THREAD_ROLES:
        typer.secho(f"--role must be one of {', '.join(THREAD_ROLES)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    chosen = _kinds(sections, THREAD_KINDS) if sections else THREAD_KINDS

    target_list = _run_sync_targets(repos or [], _kinds(None), targets)
    repo_list = [t.repo for t in target_list]

    if not export_only:
        _require_token(settings)
        if not repo_list:
            raise typer.BadParameter("name a repo, or pick a target profile with --targets")

        async def go():
            with Store(settings.db_path) as store:
                async with GraphQLClient(
                    settings.tokens, user_agent=settings.user_agent
                ) as client:
                    return await harvest_repos(
                        client,
                        store,
                        repo_list,
                        chosen,
                        max_items=max_items,
                        comments=comments,
                        page_size=page_size,
                        incremental=incremental,
                        fresh=fresh,
                        tails=tails,
                    )

        totals = _run(go())
        _report_threads(totals)

    with Store(settings.db_path) as store:
        s = store.thread_stats()
        typer.echo(
            f"stored: {s['threads']} threads {s['by_kind']} across "
            f"{s['thread_repos']} repo(s), {s['comments']} comments "
            f"({s['reviews']} reviews, {s['answers']} accepted answers)"
        )
        typer.echo(f"people: {s['people']} involved {s['by_role']}")
        if s["threads_truncated"]:
            typer.echo(
                f"        {s['threads_truncated']} thread(s) longer than --comments; "
                f"run `gitscout thread-tails` to pull their newest comments"
            )
        try:
            if out:
                count, fmt = write_threads(
                    store,
                    out,
                    repos=repo_list or None,
                    kinds=chosen,
                    role=role,
                    only_with_email=only_with_email,
                    min_confidence=min_confidence,
                    body_chars=body_chars,
                    comment_chars=comment_chars,
                )
                typer.echo(f"wrote:  {count} rows -> {out} ({fmt})")
            if comments_out:
                count, fmt = write_thread_comments(
                    store, comments_out, repos=repo_list or None, kinds=chosen
                )
                typer.echo(f"wrote:  {count} comments -> {comments_out} ({fmt})")
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc


@app.command("repo-info")
def repo_info_cmd(
    ctx: typer.Context,
    repos: Annotated[list[str], typer.Argument(help="owner/name or GitHub URL")],
) -> None:
    """Record each repository's stable id, url and headline numbers (1 point each)."""
    settings = _settings(ctx)
    _require_token(settings)

    async def go():
        with Store(settings.db_path) as store:
            async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
                return [await fetch_repo_info(client, store, r) for r in repos]

    for info in _run(go()):
        if not info:
            typer.secho("repository not found", fg=typer.colors.YELLOW)
            continue
        typer.echo(
            f"{info['repo']:36} id={info['repository_id']} stars={info['stars']} "
            f"forks={info['forks']} language={info['language']}"
        )


@app.command("participants")
def participants_cmd(ctx: typer.Context) -> None:
    """Fetch profiles for people seen only inside threads (commenters, reviewers).

    `scout --with-threads` does this automatically; run it after a standalone
    `gitscout threads` so those people can be enriched and exported.
    """
    settings = _settings(ctx)
    _require_token(settings)

    async def go():
        with Store(settings.db_path) as store:
            async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
                return (*await resolve_participants(client, store), client.points_used)

    looked, written, emails, points = _run(go())
    typer.echo(
        f"looked up {looked} thread-only people, stored {written} profiles, "
        f"+{emails} emails, {points} GraphQL points"
    )


@app.command("social")
def social_cmd(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Max people this run (0 = all)")] = 0,
    refresh: Annotated[bool, typer.Option("--refresh", help="Look up people already checked")] = False,
) -> None:
    """Fetch the LinkedIn link and public repo count people publish on their own profile.

    Only what they chose to put there (GitHub `socialAccounts`, or a LinkedIn URL in the
    website field). ~1 point per 25 people.
    """
    settings = _settings(ctx)
    _require_token(settings)

    async def go():
        with Store(settings.db_path) as store:
            async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
                looked, found = await resolve_social(client, store, limit=limit, refresh=refresh)
                return looked, found, client.points_used

    looked, found, points = _run(go())
    typer.echo(f"looked up {looked} people, {found} LinkedIn links, {points} GraphQL points")


@app.command("thread-tails")
def thread_tails_cmd(
    ctx: typer.Context,
    repo: Annotated[Optional[str], typer.Option("--repo", help="Only this repo")] = None,
    section: Annotated[Optional[str], typer.Option("--section", help=f"Only one of: {', '.join(THREAD_KINDS)}")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max threads to top up (1 query each)")] = 50,
    comments: CommentsOpt = DEFAULT_COMMENTS,
) -> None:
    """Fetch the newest comments of threads that were capped by --comments."""
    settings = _settings(ctx)
    _require_token(settings)
    if section and section not in THREAD_KINDS:
        raise typer.BadParameter(f"--section must be one of {', '.join(THREAD_KINDS)}")
    from .ingest import parse_repo

    target = parse_repo(repo) if repo else None

    async def go():
        with Store(settings.db_path) as store:
            pending = store.threads_needing_tail(target, section, limit=limit)
            if not pending:
                return 0, 0, 0
            async with GraphQLClient(
                settings.tokens, user_agent=settings.user_agent
            ) as client:
                done, points = await fetch_comment_tails(
                    client, store, target, section, limit=limit, comments=comments
                )
            return done, points, len(pending)

    done, points, pending = _run(go())
    if not pending:
        typer.echo("No capped threads waiting.")
        return
    typer.echo(f"Topped up {done} of {pending} capped thread(s) for {points} GraphQL points.")


supabase_app = typer.Typer(
    help="Mirror the local store into Supabase (Postgres) over PostgREST.",
    no_args_is_help=True,
)
app.add_typer(supabase_app, name="supabase")


@supabase_app.command("schema")
def supabase_schema(
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Write the DDL to a file instead of stdout")] = None,
) -> None:
    """Print the DDL for the mirror. Paste it into the Supabase SQL editor once.

    It is idempotent, so re-running it after an upgrade only adds what is missing.
    """
    sql = schema_sql()
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(sql, encoding="utf-8")
        typer.echo(f"Wrote {len(sql.splitlines())} lines to {out}")
        return
    typer.echo(sql)


@supabase_app.command("push")
def supabase_push(
    ctx: typer.Context,
    full: Annotated[bool, typer.Option("--full", help="Re-push everything, ignoring the watermark (use after a schema change)")] = False,
    only_with_email: Annotated[bool, typer.Option("--only-with-email", help="Push only people with a discovered address")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Cap rows per table (debugging; parents are still included)")] = 0,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would be pushed and make no network calls")] = False,
) -> None:
    """Upsert leads, addresses, threads and comments into Supabase.

    Incremental by default: only rows gitscout has touched since the last successful
    push. The watermark lives in the local database, so an interrupted push is simply
    re-run -- and it only advances once every table has landed.
    """
    settings = _settings(ctx)
    if not dry_run and not settings.supabase_enabled:
        raise typer.BadParameter(
            "Supabase is not configured. Set SUPABASE_DB_URL (direct Postgres, works "
            "even when the project's Data API is not exposed), or SUPABASE_URL plus "
            "SUPABASE_SERVICE_KEY for the REST path."
        )

    with Store(settings.db_path) as store:
        try:
            result = _run(
                supabase_push_rows(
                    settings,
                    store,
                    full=full,
                    only_with_email=only_with_email,
                    limit=limit,
                    dry_run=dry_run,
                )
            )
        except SupabaseError as exc:
            typer.secho(f"Supabase push failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc

    head = "would push" if result.dry_run else "pushed"
    typer.echo(f"{result.mode} sync, {head} {result.rows} row(s):")
    for name, count in result.counts.items():
        typer.echo(f"  {name:16} {count:>7}")
    if result.since:
        typer.echo(f"since:     {result.since}")
    if not result.dry_run:
        typer.echo(f"requests:  {result.requests} in {result.seconds}s")
        typer.echo(f"watermark: {result.watermark or '(unchanged)'}")
        _report_refresh(result)
    elif result.watermark:
        typer.echo(f"watermark would move to: {result.watermark}")


def _report_refresh(result) -> None:
    if result.refreshed:
        typer.echo(
            f"outreach:  refreshed, {result.outreach_rows} rows; "
            f"views {', '.join(result.outreach_views)}"
        )
    elif result.refresh_error:
        typer.secho(
            f"outreach:  refresh FAILED, previous dataset still served; retried next run "
            f"({result.refresh_error})",
            fg=typer.colors.YELLOW,
            err=True,
        )
    else:
        typer.echo("outreach:  unchanged (no new data)")


@supabase_app.command("status")
def supabase_status(ctx: typer.Context) -> None:
    """Compare what is here with what is in the mirror."""
    settings = _settings(ctx)
    with Store(settings.db_path) as store:
        state = store.get_crawl_state(SYNC_KEY) or {}
        local = store.sync_counts()
        pending = store.sync_counts(state.get("high_water"))

    typer.echo(
        f"configured:  {settings.supabase_transport}"
        + ("" if settings.supabase_enabled else "  (set SUPABASE_DB_URL, or SUPABASE_URL + SUPABASE_SERVICE_KEY)")
    )
    typer.echo(f"last push:   {state.get('updated_at') or 'never'}")
    typer.echo(f"watermark:   {state.get('high_water') or '(none)'}")
    typer.echo(f"{'table':16} {'local':>8} {'pending':>8}")
    for name in local:
        typer.echo(f"{name:16} {local[name]:>8} {pending[name]:>8}")

    if not settings.supabase_enabled:
        return

    def via_postgres():
        with PostgresWriter(settings.supabase_db_url or "") as db:
            return {table: db.count(table) for _n, table, _c in TARGETS}

    async def via_rest():
        async with SupabaseClient(
            settings.supabase_url or "",
            settings.supabase_key or "",
            schema=settings.supabase_schema,
        ) as client:
            out = {}
            for _name, table, _conflict in TARGETS:
                out[table] = await client.count(table)
            return out

    try:
        remote = (
            via_postgres()
            if settings.supabase_transport == "postgres"
            else _run(via_rest())
        )
    except SupabaseError as exc:
        typer.secho(f"remote: unreachable -- {exc}", fg=typer.colors.YELLOW, err=True)
        return
    typer.echo("")
    for table, count in remote.items():
        typer.echo(f"remote {table:28} {count:>8}")


@app.command()
def export(
    ctx: typer.Context,
    out: Annotated[Path, typer.Argument(help="Output path (.csv or .jsonl)")] = Path("leads.csv"),
    repo: Annotated[Optional[list[str]], typer.Option("--repo", help="Only these repos")] = None,
    fmt: Annotated[Optional[str], typer.Option("--format", help=f"Force one of: {', '.join(FORMATS)}")] = None,
    only_with_email: OnlyEmailOpt = False,
    min_confidence: MinConfOpt = 0.0,
    new_since: Annotated[Optional[str], typer.Option("--new-since", help="Only people first seen at/after this ISO timestamp")] = None,
    by_score: Annotated[bool, typer.Option("--by-score/--by-confidence", help="Row ordering")] = True,
    with_issues: Annotated[bool, typer.Option("--with-issues", help="Add each person's issues (count, title, description, comments + commenters + timestamps) as extra columns")] = False,
    with_prs: Annotated[bool, typer.Option("--with-prs", help="Same, for pull requests (includes reviews)")] = False,
    with_discussions: Annotated[bool, typer.Option("--with-discussions", help="Same, for discussions (includes the accepted answer)")] = False,
    body_chars: Annotated[int, typer.Option("--body-chars", help="Truncate each description in CSV output (0 = keep in full)")] = 0,
    comment_chars: Annotated[int, typer.Option("--comment-chars", help="Truncate each comment in CSV output (0 = keep in full)")] = 0,
    max_threads: Annotated[int, typer.Option("--max-threads", help="Spell out at most N threads per person in CSV (the count column is always complete; 0 = all)")] = 0,
) -> None:
    """Write leads to CSV or JSONL (suppressed users/emails are always excluded).

    `--with-issues` / `--with-prs` / `--with-discussions` keep the file one row per
    person and pack that person's threads into three extra columns each: a count, a
    readable digest (title, description, every comment with its author and timestamp)
    and the same thing as JSON. They need the thread stage to have run -- see
    `gitscout threads`.
    """
    settings = _settings(ctx)
    from .ingest import parse_repo

    sections = [
        name
        for name, wanted in (
            ("issues", with_issues),
            ("prs", with_prs),
            ("discussions", with_discussions),
        )
        if wanted
    ]

    with Store(settings.db_path) as store:
        if sections and not store.thread_stats()["threads"]:
            typer.secho(
                "note: no threads are stored yet, so the issue/PR/discussion columns "
                "will be empty. Run `gitscout threads -t <profile>` first.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        try:
            repos = [parse_repo(r) for r in repo] if repo else None
            count, chosen = write_export(
                store,
                out,
                fmt=fmt,
                repos=repos,
                only_with_email=only_with_email,
                min_confidence=min_confidence,
                new_since=new_since,
                order_by_score=by_score,
                sections=sections,
                body_chars=body_chars,
                comment_chars=comment_chars,
                max_threads=max_threads,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Wrote {count} rows to {out} ({chosen})")


@app.command()
def run(
    ctx: typer.Context,
    repos: Annotated[list[str], typer.Argument(help="owner/name or GitHub URL")],
    kinds: KindsOpt = None,
    max_items: MaxOpt = 0,
    fresh: FreshOpt = False,
    deep: DeepOpt = False,
    scan_websites: WebOpt = False,
    out: Annotated[Path, typer.Option("--out", "-o", help="Output CSV path")] = Path("leads.csv"),
    only_with_email: OnlyEmailOpt = False,
    min_confidence: MinConfOpt = 0.0,
) -> None:
    """The original REST pipeline: ingest -> enrich -> export. Prefer `scout`."""
    settings = _settings(ctx)
    _require_token(settings)
    res = _run(
        run_pipeline(
            settings,
            repos,
            _kinds(kinds, KINDS),
            max_items=max_items,
            fresh=fresh,
            deep=deep,
            scan_websites=scan_websites,
            out=out,
            only_with_email=only_with_email,
            min_confidence=min_confidence,
            progress=_progress,
        )
    )
    new = sum(r.new for r in res.ingest)
    typer.echo(
        f"ingested +{new} interactions | enriched {res.enrich.processed} users "
        f"({res.enrich.with_email} with email, {res.enrich.failed} failed) | "
        f"exported {res.exported} rows -> {out}"
    )


@app.command()
def score(ctx: typer.Context) -> None:
    """Recompute every lead score (0-100)."""
    with Store(_settings(ctx).db_path) as store:
        n = rescore(store)
    typer.echo(f"Scored {n} users.")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show what is in the local database."""
    with Store(_settings(ctx).db_path) as store:
        s = store.stats()
    typer.echo(f"repos:             {s['repos']}")
    typer.echo(f"interactions:      {s['interactions']}  {s['by_kind']}")
    typer.echo(f"unique users:      {s['unique_users']}")
    typer.echo(f"profiles fetched:  {s['profiles_fetched']}")
    typer.echo(f"discovery done:    {s['discovery_done']} (pending: {s['discovery_pending']})")
    typer.echo(f"users with email:  {s['users_with_email']}")
    typer.echo(f"email hit rate:    {s['hit_rate']:.1%}")
    if s["by_source"]:
        typer.echo(f"  by source:       {s['by_source']}")
    with Store(_settings(ctx).db_path) as store:
        t = store.thread_stats()
    if t["threads"]:
        typer.echo(f"threads stored:    {t['threads']}  {t['by_kind']}")
        typer.echo(
            f"thread comments:   {t['comments']} "
            f"({t['reviews']} reviews, {t['answers']} accepted answers)"
        )
        typer.echo(f"people in threads: {t['people']}  {t['by_role']}")
        if t["threads_truncated"]:
            typer.echo(f"  threads capped:  {t['threads_truncated']} (see `gitscout thread-tails`)")
    typer.echo(f"suppressed:        {s['suppressed']}")
    typer.echo(f"runs recorded:     {s['runs']}")


@app.command()
def runs(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit")] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the audit log of recent (including scheduled) runs."""
    with Store(_settings(ctx).db_path) as store:
        rows = store.recent_runs(limit)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No runs recorded yet.")
        return
    typer.echo(f"{'id':>4s} {'mode':8s} {'started':20s} {'new':>6s} {'emails':>7s} {'pts':>6s} status")
    for r in rows:
        typer.echo(
            f"{r['run_id']:>4d} {r['mode']:8s} {(r['started_at'] or '')[:19]:20s} "
            f"{r['interactions_new']:>6d} {r['emails_new']:>7d} {r['points_used']:>6d} {r['status']}"
        )


def _print_table(rows: list[dict], *, max_width: int = 38) -> None:
    """Aligned, truncated output. Good enough to read a table in a terminal."""
    if not rows:
        typer.echo("(no rows)")
        return
    cols = list(rows[0])
    text = [[("" if r.get(c) is None else str(r.get(c))).replace("\n", " ") for c in cols] for r in rows]
    widths = [
        min(max_width, max(len(c), *(len(row[i]) for row in text)))
        for i, c in enumerate(cols)
    ]
    cut = lambda s, w: s if len(s) <= w else s[: w - 1] + "…"  # noqa: E731
    typer.secho(
        "  ".join(cut(c, w).ljust(w) for c, w in zip(cols, widths)), bold=True
    )
    typer.echo("  ".join("-" * w for w in widths))
    for row in text:
        typer.echo("  ".join(cut(v, w).ljust(w) for v, w in zip(row, widths)))


@app.command("db")
def db_cmd(
    ctx: typer.Context,
    table: Annotated[Optional[str], typer.Option("--table", "-t", help="Dump this table")] = None,
    sql: Annotated[Optional[str], typer.Option("--sql", help="Read-only SQL (SELECT/WITH/PRAGMA)")] = None,
    login: Annotated[Optional[str], typer.Option("--login", help="Every email found for one person")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows (0 = all)")] = 50,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Write rows to .csv/.jsonl instead")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table")] = False,
) -> None:
    """Look inside the database. Read-only: this command cannot modify anything.

    With no options it lists every table and its row count.
    """
    settings = _settings(ctx)
    with Store(settings.db_path) as store:
        try:
            if sql:
                rows = store.read_query(sql, limit=limit or None)
            elif login:
                rows = store.emails_for(login)
            elif table:
                rows = store.table_rows(table, limit=limit or None)
            else:
                counts = store.table_counts()
                rows = [{"table": name, "rows": n} for name, n in counts.items()]
                typer.echo(f"{settings.db_path}\n")
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        if out:
            import csv as _csv

            out.parent.mkdir(parents=True, exist_ok=True)
            if out.suffix.lower() in (".jsonl", ".ndjson"):
                with out.open("w", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            else:
                with out.open("w", newline="", encoding="utf-8") as fh:
                    writer = _csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["empty"])
                    writer.writeheader()
                    writer.writerows(rows)
            typer.echo(f"Wrote {len(rows)} rows to {out}")
            return

    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    _print_table(rows)
    if not table and not sql and not login:
        typer.echo("\nTry: gitscout db --table users -n 20   |   gitscout db --sql \"SELECT ...\"")
    elif limit and len(rows) == limit:
        typer.echo(f"\n({limit} row limit reached; use -n 0 for all, or -o out.csv)")


@app.command()
def suppress(
    ctx: typer.Context,
    values: Annotated[list[str], typer.Argument(help="GitHub logins or email addresses")],
) -> None:
    """Add logins/emails to the do-not-contact list (excluded from every export)."""
    with Store(_settings(ctx).db_path) as store:
        n = store.suppress(values)
    typer.echo(f"Added {n} suppression entr{'y' if n == 1 else 'ies'}.")


@app.command("rate-limit")
def rate_limit(ctx: typer.Context) -> None:
    """Show remaining REST quota and GraphQL points for each configured token."""
    settings = _settings(ctx)
    _require_token(settings)

    async def go():
        from .queries import VIEWER

        async with GitHubClient(settings.tokens, user_agent=settings.user_agent) as client:
            rest = await client.rate_limits()
        async with GraphQLClient(settings.tokens, user_agent=settings.user_agent) as client:
            gql = await client.execute(VIEWER)
        return rest, gql

    rest, gql = _run(go())
    for row in rest:
        typer.echo(f"REST    {row}")
    typer.echo(f"GraphQL remaining={gql.remaining} limit={gql.data.get('rateLimit', {}).get('limit')} resets={gql.reset_at}")


if __name__ == "__main__":
    app()
