"""The worker inside the dashboard process: one service instead of two.

For a host where a background worker costs extra (Render's free plan has web services
only). The worker loop runs on its own thread and event loop; the dashboard's
shutdown stops it the same way SIGTERM stops `gitscout worker run`, so a running job
is recorded as failed with a retry queued.

A free web service sleeps after 15 minutes without inbound traffic, which would kill a
run halfway. While a job runs, the worker requests its own public /healthz every four
minutes; that is inbound traffic, so the host stays up exactly as long as there is
work. Waking it for the nightly schedule is done from outside (see docs/deploy.md).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

import httpx

from ..config import Settings
from ..worker import Worker

log = logging.getLogger(__name__)
KEEPALIVE_SECONDS = 240


class EmbeddedWorker:
    def __init__(self, settings: Settings, *, public_url: str | None = None) -> None:
        self.worker = Worker(settings)
        self.public_url = (public_url or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gitscout-worker", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        self._ready.set()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        keepalive = asyncio.create_task(self._keepalive())
        try:
            await self.worker.serve(self._stop)
        finally:
            keepalive.cancel()

    async def _keepalive(self) -> None:
        if not self.public_url:
            return
        async with httpx.AsyncClient(timeout=20) as http:
            while True:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                if not self.worker.busy:
                    continue
                try:
                    await http.get(self.public_url + "/healthz")
                except httpx.HTTPError as exc:
                    log.debug("keepalive failed: %s", exc)

    def stop(self, timeout: float = 25) -> None:
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout)
