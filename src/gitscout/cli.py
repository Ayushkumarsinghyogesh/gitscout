"""Command line interface:  gitscout scout --targets cloud-security -o leads.csv"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Optional, Sequence

import typer

from .config import Settings, load_settings
from .enrich import Enricher
from .enrich_gql import GqlEnricher
from .export import FORMATS, write_export
from .github_client import GitHubClient, GitHubError
from .graphql import GraphQLClient
from .models import ALL_KINDS, KINDS
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
from .storage import Store
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
KindsOpt = Annotated[str, typer.Option("--kinds", "-k", help=f"Comma list of: {', '.join(ALL_KINDS)}")]
MaxOpt = Annotated[int, typer.Option("--max", help="Soft cap per repo+kind per run (0 = unlimited)")]
FreshOpt = Annotated[bool, typer.Option("--fresh", help="Ignore saved cursors and re-crawl")]
DeepOpt = Annotated[bool, typer.Option("--deep", help="Run every email source, not just until first hit")]
WebOpt = Annotated[bool, typer.Option("--scan-websites", help="Also scan the site linked from profiles")]
OnlyEmailOpt = Annotated[bool, typer.Option("--only-with-email", help="Export only rows with an email")]
MinConfOpt = Annotated[float, typer.Option("--min-confidence", help="Drop emails scoring below this (0-1)")]
TargetsOpt = Annotated[Optional[str], typer.Option("--targets", "-t", help="Target profile (name or path)")]
OutOpt = Annotated[Optional[list[Path]], typer.Option("--out", "-o", help="Output file (repeatable; .csv or .jsonl)")]


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj


def _kinds(value: str, allowed: Sequence[str] = ALL_KINDS) -> tuple[str, ...]:
    try:
        return parse_kinds(value, allowed)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


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


def _report_scout(result: ScoutResult) -> None:
    for r in result.ingest.results:
        total = f"/{r.total_count}" if r.total_count else ""
        typer.echo(
            f"{r.repo:40s} {r.kind:7s} fetched={r.fetched:<6d}{total:<9s} "
            f"new={r.new:<6d} {r.status}"
        )
    e = result.enrich
    typer.echo(
        f"\nemails: +{result.ingest.emails_new} from profiles, "
        f"+{e.emails_new} from commits/websites"
        + (f", +{result.provider_hits} from providers" if result.provider_calls else "")
    )
    typer.echo(
        f"cost:   {result.points} GraphQL points "
        f"({result.ingest.points} ingest + {e.points} enrich, {e.queries} batched queries)"
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
    kinds: KindsOpt = ",".join(ALL_KINDS),
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
) -> None:
    """GraphQL pipeline: ingest -> email discovery -> score -> export. The main command."""
    settings = _settings(ctx)
    _require_token(settings)
    target_list = _run_sync_targets(repos or [], _kinds(kinds), targets)
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
            progress=_progress,
        )
    )
    _report_scout(result)


def _run_sync_targets(repos: list[str], kinds: tuple[str, ...], targets_file: str | None):
    try:
        return targets_for(repos, kinds, targets_file)
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def watch(
    ctx: typer.Context,
    repos: RepoArgs = None,
    targets: TargetsOpt = None,
    kinds: KindsOpt = ",".join(ALL_KINDS),
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
    target_list = _run_sync_targets(repos or [], _kinds(kinds), targets)

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
    kinds: KindsOpt = ",".join(ALL_KINDS),
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
    kinds: KindsOpt = "stars",
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
) -> None:
    """Write leads to CSV or JSONL (suppressed users/emails are always excluded)."""
    settings = _settings(ctx)
    from .ingest import parse_repo

    with Store(settings.db_path) as store:
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
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Wrote {count} rows to {out} ({chosen})")


@app.command()
def run(
    ctx: typer.Context,
    repos: Annotated[list[str], typer.Argument(help="owner/name or GitHub URL")],
    kinds: KindsOpt = ",".join(KINDS),
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
