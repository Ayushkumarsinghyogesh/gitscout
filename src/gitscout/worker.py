"""The always-on worker: turns queued jobs into runs, with nobody watching.

One process, one loop, forever:

    every tick (default 60 s):
        fail running jobs whose heartbeat stopped, queue their retries
        enqueue a job for every repository whose schedule matches this minute
        claim the next due job and run it: crawl -> enrich -> social -> push -> refresh
        record the outcome (counters, checkpoint, GitHub points, error); retry on failure

What makes it safe to leave alone:

* **The queue is the truth.** The loop holds no state. Kill it mid-run and restart it:
  the job's heartbeat stops, the next tick fails it as stalled and a retry is queued.
  Two workers can run at once; the database hands each job to one of them.
* **Heartbeats are real.** A side task writes ``heartbeat_at`` every 30 s while the
  job runs, and reads the job's status back, so a cancellation from the dashboard
  stops the run within a heartbeat.
* **Tokens do not expire.** With a GitHub App configured the worker mints installation
  tokens and hands the client a refresh hook; with plain PATs it uses them as given.
* **A lost disk is not a re-backfill.** After every successful run the repository's
  crawl cursors are copied to ``repositories.checkpoint``; a worker starting with an
  empty local store restores them before crawling.
* **Shutdown is clean.** SIGTERM (what Render sends on deploy) cancels the running job,
  records it as failed with a clear reason, and lets the retry pick up where the
  cursors left off.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import httpx

from .alerts import Alerter, for_stalled
from .config import Settings
from .github_app import AppCredentials, InstallationTokens
from .graphql import GraphQLClient
from .jobs import Job, JobError, JobStore, RepoConfig, code_version, cron_matches, sanitize, worker_name
from .lock import run_lock
from .logs import job_context
from .models import THREAD_KINDS
from .pipeline import ScoutResult, run_scout
from .storage import Store
from .sweep import sweep_repo
from .supabase import SupabaseError, SyncResult
from .supabase import push as supabase_push
from .targets import targets_from_repos
from .threads_gql import fetch_repo_info, resolve_social

log = logging.getLogger(__name__)

#: `collections` as the configuration names them (what the dashboard shows) -> the
#: ingest stage's kind names. `commits` is `contribs` inside the engine.
INTERACTION_KINDS = {"forks": "forks", "issues": "issues", "prs": "prs",
                     "discussions": "discussions", "commits": "contribs", "stars": "stars"}
DEFAULT_COLLECTIONS = ("issues", "prs", "discussions", "forks", "commits")
#: When the deletion sweep runs unless `settings.sweep_cron` says otherwise: Sunday 01:00
#: in the repository's time zone, an hour after the nightly run at most.
DEFAULT_SWEEP_CRON = "0 1 * * 0"
#: The nightly all-time export into Storage, unless `settings.export_cron` says otherwise:
#: 02:00 local, two hours after the nightly run starts.
DEFAULT_EXPORT_CRON = "0 2 * * *"


def _last_key(cron_key: str) -> str:
    """settings key remembering the minute a periodic job was last enqueued; the sweep's
    is `last_sweep_minute`, as before."""
    return "last_" + cron_key.replace("_cron", "_minute")


def engine_kinds(collections: Sequence[str]) -> list[str]:
    """The ingest kinds for a repository's collections, in order, unknown names dropped."""
    return [INTERACTION_KINDS[c] for c in collections if c in INTERACTION_KINDS]


@dataclass
class Outcome:
    """What a job ended as, in the shape JobStore.finish wants."""

    status: str
    counters: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    rate: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def counters_from(result: ScoutResult, pushed: SyncResult | None = None) -> dict[str, Any]:
    """Per-collection counts the dashboard shows. Zero rows and a failure are
    distinguishable because `status` lives on the job, not in here."""
    out: dict[str, Any] = {
        "interactions": {"discovered": result.ingest.new, "points": result.ingest.points},
        "people": {"new": sum(r.profiles for r in result.ingest.results)},
        "emails": {"new": result.emails_new},
        "threads": {
            "fetched": sum(r.fetched for r in result.threads.results), "new": result.threads.new,
            "comments": result.threads.comments_new, "filled": result.threads_filled,
            "points": result.threads.points + result.thread_fill_points,
        },
        "enrich": {"processed": result.enrich.processed, "emails": result.enrich.emails_new,
                   "points": result.enrich.points},
    }
    if pushed is not None:
        out["push"] = {**{k: v for k, v in pushed.counts.items()}, "rows": pushed.rows,
                       "seconds": pushed.seconds, "refreshed": bool(pushed.refreshed)}
    return out


def restore_checkpoint(local: Store, repo: str, checkpoint: Mapping[str, Any]) -> int:
    """Write the cursors held in Postgres into an empty local store. Existing local
    state wins: it is newer than what was copied up at the end of the last run."""
    restored = 0
    for key, state in (checkpoint or {}).items():
        if repo not in key or local.get_crawl_state(key) is not None:
            continue
        local.set_crawl_state(
            key, cursor=state.get("cursor"), done=state.get("done"),
            high_water=state.get("high_water"), total_count=state.get("total_count"),
        )
        restored += 1
    return restored


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        name: str | None = None,
        version: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        app_api: str = "https://api.github.com",
        clock: Callable[[], datetime] | None = None,
        alert_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.supabase_db_url:
            raise JobError("the worker needs SUPABASE_DB_URL: the queue lives in Postgres")
        self.settings = settings
        self.name = name or worker_name()
        self.version = version or code_version()
        self.transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._app: InstallationTokens | None = None
        creds = AppCredentials.from_settings(settings)
        if creds is not None:
            self._app = InstallationTokens(creds, api=app_api, transport=transport)
        elif not settings.tokens:
            raise JobError("no GitHub credentials: set GITHUB_TOKENS or the GITHUB_APP_* variables")
        self.jobs_run = 0
        self.alerter = Alerter(settings, transport=alert_transport, clock=self._clock)
        self.storage_transport: httpx.BaseTransport | None = None
        #: True while a job runs; the embedded mode keeps a sleeping host awake on it
        self.busy = False

    def _alerting(self, store: JobStore, check: Callable[[], Any]) -> None:
        """Run an alert check and commit it on its own; never let it touch the run."""
        try:
            check()
            store.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("alert check failed: %s", sanitize(str(exc)))
            try:
                store._conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # --- credentials ---------------------------------------------------------
    async def _token_refresh(self) -> list[str] | None:
        return await self._app.refresh() if self._app is not None else None

    async def _settings_for_run(self) -> Settings:
        if self._app is None:
            return self.settings
        return replace(self.settings, tokens=(await self._app.current(),))

    # --- one job -------------------------------------------------------------
    async def run_job(self, store: JobStore, job: Job, cfg: RepoConfig) -> Outcome:
        """Run one claimed job to completion while heart-beating. Never raises: every
        way a run can end comes back as an Outcome."""
        counters: dict[str, Any] = {}
        cancelled = asyncio.Event()
        main = asyncio.current_task()
        assert main is not None

        async def beat() -> None:
            while True:
                await asyncio.sleep(self.settings.worker_heartbeat_seconds)
                try:
                    status = await asyncio.to_thread(self._heartbeat, store, job.id, counters)
                except Exception as exc:  # noqa: BLE001 - a missed beat is not a failed run
                    log.warning("heartbeat for job %s failed: %s", job.id, sanitize(str(exc)))
                    continue
                if status == "cancelled":
                    cancelled.set()
                    main.cancel()
                    return

        context = job_context.set({"job_id": job.id, "repo": job.repo, "kind": job.kind})
        beater = asyncio.create_task(beat())
        try:
            # The same file lock `gitscout scout --push` takes: a manual run on this
            # machine and the worker can never interleave writes to the local store.
            with run_lock(self.settings.db_path):
                return await self._execute(job, cfg, counters)
        except asyncio.CancelledError:
            if cancelled.is_set():
                return Outcome("cancelled", counters, error="cancelled from the dashboard")
            # The worker itself is shutting down (SIGTERM): record it and let the
            # retry continue from the saved cursors. Keep the cancellation pending
            # for the caller by re-raising after the outcome is recorded.
            return Outcome("failed", counters, error="worker shutting down; retry scheduled")
        except Exception as exc:  # noqa: BLE001 - the queue records it; the loop goes on
            log.exception("job %s failed", job.id)
            return Outcome("failed", counters, error=f"{type(exc).__name__}: {sanitize(str(exc))}")
        finally:
            beater.cancel()
            job_context.reset(context)

    def _heartbeat(self, store: JobStore, job_id: int, counters: Mapping[str, Any]) -> str:
        status = store.heartbeat(job_id, counters=dict(counters))
        store.seen(self.name, self.version, jobs_run=self.jobs_run, current_job_id=job_id)
        store.commit()
        return status

    async def _execute(self, job: Job, cfg: RepoConfig, counters: dict[str, Any]) -> Outcome:
        if job.kind == "export":
            return await asyncio.to_thread(self._execute_export, job, counters)
        settings = await self._settings_for_run()
        if job.kind == "sweep":
            return await self._execute_sweep(settings, job, cfg, counters)
        collections = list(cfg.collections) or list(DEFAULT_COLLECTIONS)
        kinds = engine_kinds(collections)
        thread_kinds = [k for k in collections if k in THREAD_KINDS]
        max_items = int(cfg.settings.get("max") or 0)
        limit = int(cfg.settings.get("limit") or 0)
        comments = int(cfg.settings.get("comments") or 0)

        with Store(settings.db_path) as local:
            restored = restore_checkpoint(local, job.repo, cfg.checkpoint)
            if restored:
                log.info("job %s: restored %d cursors for %s from Postgres", job.id, restored, job.repo)

        targets = targets_from_repos([job.repo], kinds or engine_kinds(DEFAULT_COLLECTIONS))
        result = await run_scout(
            settings, targets,
            max_items=max_items,
            fresh=job.kind in ("full", "backfill"),
            incremental=job.kind == "incremental",
            enrich_limit=limit or None,
            with_threads=bool(thread_kinds),
            thread_kinds=thread_kinds or THREAD_KINDS,
            **({"thread_comments": comments} if comments else {}),
            transport=self.transport,
            token_refresh=self._token_refresh,
        )
        counters.update(counters_from(result))
        if result.status != "ok":
            raise RuntimeError(result.error or "the crawl did not finish")

        with Store(settings.db_path) as local:
            async with GraphQLClient(
                settings.tokens, user_agent=settings.user_agent, transport=self.transport,
                token_refresh=self._token_refresh,
            ) as client:
                social = await resolve_social(client, local) if "social" not in cfg.settings.get("skip", []) else None
                await fetch_repo_info(client, local, job.repo)
                extra_points = client.points_used
                state = client.pool.states[0]
                remaining, reset_at = state.remaining, state.reset_at
            if social:
                counters["social"] = {"looked_up": social[0], "linkedin": social[1]}
            pushed = await supabase_push(settings, local)
            checkpoint = local.crawl_states(job.repo)

        counters.update(counters_from(result, pushed))
        rate = {
            "points_used": result.points + extra_points,
            "remaining": remaining,
            "reset_at": datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat() if reset_at else None,
        }
        if pushed.refresh_error:
            # The data landed; only the dataset refresh failed. Surface it, do not
            # fail the run: the next push retries the refresh.
            counters["push"]["refresh_error"] = sanitize(pushed.refresh_error)
        return Outcome("succeeded", counters, checkpoint, rate)

    async def _execute_sweep(self, settings: Settings, job: Job, cfg: RepoConfig,
                             counters: dict[str, Any]) -> Outcome:
        """Find what GitHub no longer lists and mark it; then push so the dataset
        leaves it out. Nothing is deleted locally or remotely."""
        thread_kinds = [k for k in (cfg.collections or DEFAULT_COLLECTIONS) if k in THREAD_KINDS]
        with Store(settings.db_path) as local:
            async with GraphQLClient(
                settings.tokens, user_agent=settings.user_agent, transport=self.transport,
                token_refresh=self._token_refresh,
            ) as client:
                report = await sweep_repo(client, local, job.repo, thread_kinds or THREAD_KINDS)
                state = client.pool.states[0]
                remaining, reset_at, points = state.remaining, state.reset_at, client.points_used
            counters["sweep"] = report
            pushed = await supabase_push(settings, local)
        counters["push"] = {**pushed.counts, "rows": pushed.rows, "seconds": pushed.seconds,
                            "refreshed": bool(pushed.refreshed)}
        incomplete = [k for k, r in report.items() if not r.get("complete")]
        if incomplete:
            raise RuntimeError(f"sweep could not list every {', '.join(incomplete)}; nothing was marked for those")
        rate = {"points_used": points, "remaining": remaining,
                "reset_at": datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat() if reset_at else None}
        return Outcome("succeeded", counters, None, rate)

    def _execute_export(self, job: Job, counters: dict[str, Any]) -> Outcome:
        """The nightly archive: every record of the repository, gzipped into Storage."""
        from .exports import Storage, nightly_export

        if not (self.settings.supabase_url and self.settings.supabase_key):
            raise JobError("exports need SUPABASE_URL and SUPABASE_SERVICE_KEY for Storage")
        storage = Storage(self.settings.supabase_url, self.settings.supabase_key,
                          transport=self.storage_transport)
        with JobStore(self.settings.supabase_db_url, schema=self.settings.supabase_schema) as store:
            retention = int(store.settings().get("export_retention_days") or 90)
            counters.update(nightly_export(self.settings.supabase_db_url, store, storage, job.repo,
                                           job_id=job.id, retention_days=retention, clock=self._clock))
        return Outcome("succeeded", counters, None, {})

    def _periodic_due(self, store: JobStore, now: datetime, cron_key: str, default: str) -> list[RepoConfig]:
        """Enabled repositories whose `settings[cron_key]` (else `default`) matches this
        minute, once per minute: the minute is remembered in settings[last_<cron_key>]."""
        from zoneinfo import ZoneInfo

        due = []
        minute = now.replace(second=0, microsecond=0).isoformat()
        for cfg in store.repositories(enabled_only=True):
            expr = str(cfg.settings.get(cron_key) or default)
            try:
                if not cron_matches(expr, now.astimezone(ZoneInfo(cfg.timezone))):
                    continue
            except Exception:  # noqa: BLE001 - a bad expression skips, never stops the loop
                continue
            if cfg.settings.get(_last_key(cron_key)) == minute:
                continue
            due.append(cfg)
        return due

    def _sweeps_due(self, store: JobStore, now: datetime) -> list[RepoConfig]:
        return self._periodic_due(store, now, "sweep_cron", DEFAULT_SWEEP_CRON)

    def _exports_due(self, store: JobStore, now: datetime) -> list[RepoConfig]:
        if not (self.settings.supabase_url and self.settings.supabase_key):
            return []
        return self._periodic_due(store, now, "export_cron", DEFAULT_EXPORT_CRON)

    # --- the loop ------------------------------------------------------------
    async def tick(self) -> bool:
        """One pass: recover, schedule, run at most one job. True when a job ran."""
        stale_minutes = 10
        with JobStore(self.settings.supabase_db_url, schema=self.settings.supabase_schema) as store:
            try:
                stale_minutes = int(store.settings().get("stale_job_minutes") or 10)
            except Exception:  # noqa: BLE001 - defaults are fine
                pass
            store.seen(self.name, self.version, jobs_run=self.jobs_run)
            stalled = store.requeue_stale(timedelta(minutes=stale_minutes))
            for job_id, retry in stalled:
                log.warning("job %s had no heartbeat for %d min; retry %s", job_id, stale_minutes, retry)
                store.audit("job.stalled", target_kind="job", target_id=str(job_id),
                            details={"retry": retry}, result="failed")
            store.commit()
            if stalled:
                self._alerting(store, lambda: self.alerter.raise_all(
                    store, [for_stalled(store.get(j), j, r) for j, r in stalled]))
            self._alerting(store, lambda: self.alerter.check_overdue(store))
            now = self._clock()
            for kind, cron_key, due in (("sweep", "sweep_cron", self._sweeps_due(store, now)),
                                        ("export", "export_cron", self._exports_due(store, now))):
                for cfg in due:
                    job_id = store.enqueue(cfg.repo, kind, "schedule", collections=cfg.collections, priority=200)
                    fresh = next((r.settings for r in store.repositories() if r.repo == cfg.repo), cfg.settings)
                    store.set_repository(cfg.repo, settings={**fresh,
                                         _last_key(cron_key): now.replace(second=0, microsecond=0).isoformat()})
                    log.info("%s scheduled for %s: job %s", kind, cfg.repo, job_id)
            for cfg, minute in store.due(now):
                job_id = store.enqueue(cfg.repo, "incremental", "schedule", collections=cfg.collections)
                store.mark_scheduled(cfg.repo, minute)
                if job_id is not None:
                    log.info("scheduled %s: job %s", cfg.repo, job_id)
                else:
                    log.info("scheduled %s: a job is already queued or running; skipped", cfg.repo)
                    store.audit("job.schedule_skipped", repo=cfg.repo, result="skipped")
            store.commit()

            job = store.claim(self.name, self.version)
            if job is None:
                return False
            store.commit()
            cfg = next((r for r in store.repositories() if r.repo == job.repo), None)
            if cfg is None:
                store.finish(job.id, "failed", error="repository is no longer registered")
                return True
            log.info("job %s: %s %s (attempt %d/%d)", job.id, job.kind, job.repo, job.attempt, job.max_attempts)
            self.busy = True
            try:
                outcome = await self.run_job(store, job, cfg)
            finally:
                self.busy = False
            retry = store.finish(
                job.id, outcome.status, counters=outcome.counters, checkpoint=outcome.checkpoint,
                rate=outcome.rate, error=outcome.error,
            )
            store.audit(f"job.{outcome.status}", repo=job.repo, target_kind="job", target_id=str(job.id),
                        details={"retry": retry, "kind": job.kind}, result=outcome.status)
            self.jobs_run += 1
            store.seen(self.name, self.version, jobs_run=self.jobs_run)
            store.commit()
            log.info("job %s %s%s", job.id, outcome.status, f"; retry {retry}" if retry else "")
            self._alerting(store, lambda: self.alerter.after_job(
                store, job, outcome.status, retry=retry, error=outcome.error,
                counters=outcome.counters, rate=outcome.rate))
            return True

    async def serve(self, stop: asyncio.Event | None = None, *, once: bool = False) -> None:
        """Run ticks until `stop` is set. Between empty ticks, wait the poll interval."""
        stop = stop or asyncio.Event()
        log.info("worker %s (%s) started; poll every %ds", self.name, self.version, self.settings.worker_poll_seconds)
        while not stop.is_set():
            tick = asyncio.create_task(self.tick())
            stopper = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({tick, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if tick in done:
                stopper.cancel()
                try:
                    ran = tick.result()
                except Exception:  # noqa: BLE001 - never let one bad tick end the service
                    log.exception("tick failed; continuing")
                    ran = False
            else:
                tick.cancel()
                try:
                    await tick
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                break
            if once:
                return
            if not ran:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass
        log.info("worker %s stopped", self.name)
        try:
            with JobStore(self.settings.supabase_db_url, schema=self.settings.supabase_schema) as store:
                store.seen(self.name, self.version, jobs_run=self.jobs_run, stopped=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the stop: %s", sanitize(str(exc)))
        if self._app is not None:
            await self._app.aclose()
