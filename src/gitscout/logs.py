"""Logging for the unattended worker: one JSON object per line, tagged with the job.

``GITSCOUT_LOG_FORMAT=json`` switches the worker to this format, which is what a
hosting log viewer (Render) can filter on: ``job_id``, ``repo`` and ``kind`` ride on
every line logged while a job runs, without each call site passing them. Messages
pass through ``sanitize`` so a token or DSN password in an exception never reaches
the log.
"""
from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .jobs import sanitize

#: Set by the worker for the duration of a job: {"job_id": .., "repo": .., "kind": ..}
job_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("gitscout_job", default=None)


class JobContextFilter(logging.Filter):
    """Copies the current job's fields onto every record (text format shows them too)."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = job_context.get() or {}
        record.job = f"[job {ctx['job_id']}] " if ctx.get("job_id") else ""
        for key in ("job_id", "repo", "kind"):
            setattr(record, key, ctx.get(key))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": sanitize(record.getMessage(), 4000),
        }
        for key in ("job_id", "repo", "kind"):
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = sanitize(self.formatException(record.exc_info), 8000)
        return json.dumps(out, default=str)


def configure(fmt: str = "text", level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(JobContextFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(job)s%(message)s"))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
