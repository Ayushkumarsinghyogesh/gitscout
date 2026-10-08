"""The cron layer: run a scout on a schedule.

Three ways to schedule, all driving the same code path:

* ``gitscout watch --every 6h`` -- a long-lived process. Cross-platform, and what the
  Docker image runs as its entrypoint.
* ``gitscout scout --incremental --once`` -- a single run, for an external scheduler:
  GitHub Actions, Windows Task Scheduler, systemd timers, k8s CronJob.
* Both do the same work. ``watch`` is just a loop around the single run, with jitter
  and crash-tolerance.

Every scheduled run is **incremental**: ordered newest-first and halted at the
high-water mark from last time, so a 15-minute cadence costs a handful of GraphQL
points per repo rather than re-walking the whole stargazer list.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .config import Settings
from .models import Target
from .pipeline import ScoutResult, run_scout
from .storage import Store

log = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$", re.I)
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

MIN_INTERVAL = 60.0


def parse_interval(value: str | float | int) -> float:
    """'15m' -> 900.0. A bare number is minutes, which is the least surprising default."""
    if isinstance(value, (int, float)):
        seconds = float(value) * 60.0
    else:
        match = _INTERVAL_RE.match(str(value))
        if not match:
            raise ValueError(
                f"cannot read interval {value!r}; use forms like 30s, 15m, 6h, 1d"
            )
        amount = float(match.group(1))
        unit = (match.group(2) or "m").lower()
        seconds = amount * _UNITS[unit]
    if seconds < MIN_INTERVAL:
        raise ValueError(f"interval must be at least {int(MIN_INTERVAL)}s (got {value!r})")
    return seconds


def format_interval(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


@dataclass
class WatchState:
    """Live counters for the watch loop, also used by the tests."""

    runs: int = 0
    failures: int = 0
    interactions_new: int = 0
    emails_new: int = 0
    points: int = 0
    last_error: str | None = None
    history: list[ScoutResult] = field(default_factory=list)

    def record(self, result: ScoutResult) -> None:
        self.runs += 1
        self.interactions_new += result.ingest.new
        self.emails_new += result.emails_new
        self.points += result.points
        self.history.append(result)


def next_run_at(seconds: float, *, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(seconds=seconds)


async def run_once(
    settings: Settings,
    targets: Sequence[Target],
    *,
    outputs: Sequence[str | Path] = (),
    new_only: bool = True,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    enrich_limit: int | None = None,
    scan_websites: bool = False,
    mode: str = "watch",
    **kw: Any,
) -> ScoutResult:
    """One incremental scout pass. This is what every scheduler backend calls."""
    return await run_scout(
        settings,
        targets,
        incremental=True,
        enrich=True,
        enrich_limit=enrich_limit,
        scan_websites=scan_websites,
        outputs=outputs,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        new_only=new_only,
        mode=mode,
        **kw,
    )


async def watch(
    settings: Settings,
    targets: Sequence[Target],
    *,
    every: str | float = "6h",
    jitter: float = 0.1,
    max_runs: int = 0,
    outputs: Sequence[str | Path] = (),
    new_only: bool = True,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    enrich_limit: int | None = None,
    scan_websites: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_result: Callable[[ScoutResult], None] | None = None,
    stop: asyncio.Event | None = None,
    **kw: Any,
) -> WatchState:
    """Loop a scout run forever (or `max_runs` times).

    A failed run is logged and retried on the next tick rather than killing the loop:
    an unattended scraper that dies on one transient GitHub 502 is useless.
    """
    interval = parse_interval(every)
    state = WatchState()
    stop = stop or asyncio.Event()

    log.info(
        "watching %d repo(s) every %s; first run now",
        len(targets),
        format_interval(interval),
    )

    while not stop.is_set():
        try:
            result = await run_once(
                settings,
                targets,
                outputs=outputs,
                new_only=new_only,
                only_with_email=only_with_email,
                min_confidence=min_confidence,
                enrich_limit=enrich_limit,
                scan_websites=scan_websites,
                **kw,
            )
            state.record(result)
            log.info(
                "run %d: +%d interactions, +%d emails, %d points",
                state.runs,
                result.ingest.new,
                result.emails_new,
                result.points,
            )
            if on_result:
                on_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep watching across failures
            state.failures += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            log.error("scheduled run failed: %s", state.last_error)

        if max_runs and state.runs + state.failures >= max_runs:
            break
        if stop.is_set():
            break

        delay = interval * (1 + random.uniform(-jitter, jitter)) if jitter else interval
        log.info("next run in %s", format_interval(round(delay)))
        waiter = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({waiter}, timeout=delay)
        finally:
            waiter.cancel()

    log.info("watch stopped after %d run(s), %d failure(s)", state.runs, state.failures)
    return state


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Make Ctrl-C and `docker stop` end the loop cleanly instead of mid-write."""
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows asyncio has no add_signal_handler; KeyboardInterrupt covers it.
            pass


def run_log(settings: Settings, limit: int = 10) -> list[dict[str, Any]]:
    with Store(settings.db_path) as store:
        return store.recent_runs(limit)
