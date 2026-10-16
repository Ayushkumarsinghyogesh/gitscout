"""Alerts: tell a person when the unattended system needs one, and only then.

The worker calls this after every job and every tick. Each rule produces an
``Alert`` with a *key*; the key decides deduplication: one email per key per
``alert_cooldown_minutes`` (app_settings, default 6 h), however often the condition
fires. Every alert is written to ``gitscout.alerts`` whether or not mail went out, so
the history is complete even before email is configured.

Rules:

=====================  ========  ============================================
key                    severity  when
=====================  ========  ============================================
job_failed:<repo>:<k>  critical  a job failed and has no attempts left
auth                   critical  GitHub refused the credentials (401 / App token)
stalled:<repo>         warning   a running job's heartbeat stopped (worker died)
rate:<repo>            warning   a run ended with fewer points left than the floor
refresh:<repo>         warning   data landed but the dataset refresh failed
overdue:<repo>         warning   an enabled repository has no success for overdue_hours
recovered:<repo>:<k>   info      a success after a job_failed alert
=====================  ========  ============================================

Mail goes through Resend's HTTP API (``RESEND_API_KEY``); recipients are
``app_settings.alert_emails``, else ``GITSCOUT_ALERT_EMAILS``. Nothing in here may
break a run: every failure is logged and recorded, never raised.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import httpx

from .config import Settings
from .jobs import Job, JobStore, sanitize

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
_AUTH = re.compile(r"bad credentials|githubautherror|\b401\b|installation token", re.I)


@dataclass
class Alert:
    key: str
    severity: str
    subject: str
    body: str
    repo: str | None = None
    job_id: int | None = None


def _setting(values: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(values.get(key, default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- rules


def for_finished_job(job: Job, status: str, *, retry: int | None, error: str | None,
                     counters: Mapping[str, Any], rate: Mapping[str, Any],
                     rate_floor: float = 250) -> list[Alert]:
    """Alerts a finished job raises. Pure: the caller records and sends them."""
    out: list[Alert] = []
    where = f"{job.kind} job {job.id} for {job.repo}"
    if status == "failed" and error and _AUTH.search(error):
        out.append(Alert(
            "auth", "critical", "GitHub refused the worker's credentials",
            f"{where} failed because GitHub refused the credentials:\n\n{error}\n\n"
            "Every repository is affected until this is fixed: check the GitHub App "
            "installation (or the tokens) on the worker.", job.repo, job.id))
    if status == "failed" and retry is None:
        out.append(Alert(
            f"job_failed:{job.repo}:{job.kind}", "critical", f"{job.repo}: {job.kind} run failed",
            f"{where} failed on attempt {job.attempt} of {job.max_attempts}; no retry is left.\n\n"
            f"Error: {error or 'unknown'}\n\nThe next scheduled run will try again. "
            f"Inspect it with: gitscout jobs show {job.id}", job.repo, job.id))
    if status == "succeeded":
        remaining = rate.get("remaining")
        if isinstance(remaining, (int, float)) and remaining < rate_floor:
            out.append(Alert(
                f"rate:{job.repo}", "warning", f"{job.repo}: GitHub rate limit nearly used up",
                f"{where} finished with {int(remaining)} GitHub points left (floor {int(rate_floor)}); "
                f"the budget resets at {rate.get('reset_at') or 'the top of the hour'}. "
                "Runs back off on their own; repeated alerts mean the schedule asks for more "
                "than one identity can give.", job.repo, job.id))
        refresh = (counters.get("push") or {}).get("refresh_error")
        if refresh:
            out.append(Alert(
                f"refresh:{job.repo}", "warning", f"{job.repo}: dataset refresh failed",
                f"{where} pushed its data but refreshing the outreach dataset failed:\n\n{refresh}\n\n"
                "The next push retries the refresh.", job.repo, job.id))
    return out


def for_stalled(job: Job | None, job_id: int, retry: int | None) -> Alert:
    repo = job.repo if job else "unknown"
    return Alert(
        f"stalled:{repo}", "warning", f"{repo}: a run stopped responding",
        f"Job {job_id} for {repo} stopped sending heartbeats; the worker running it is presumed "
        f"dead. {'Retry job ' + str(retry) + ' is queued.' if retry else 'No attempts are left.'}",
        repo, job_id)


def for_overdue(row: Mapping[str, Any], hours: float) -> Alert:
    repo = row["repo"]
    since = row.get("last_success_at")
    return Alert(
        f"overdue:{repo}", "warning", f"{repo}: no successful run in {int(hours)} hours",
        f"{repo} is enabled (schedule {row.get('schedule_cron')} {row.get('timezone')}) but its last "
        f"success was {since.isoformat() if since else 'never'}. Last job: "
        f"{row.get('last_job_id') or '-'} {row.get('last_job_status') or ''} {row.get('last_job_error') or ''}\n\n"
        "Check that the worker is running: gitscout health", repo, row.get("last_job_id"))


# ------------------------------------------------------------------ sending


def _safe(store: JobStore, sql: str, params: Sequence[Any] = ()) -> list[tuple] | None:
    """A read that cannot abort the surrounding transaction; None when it failed."""
    try:
        store._rows("savepoint alert_read")
        rows = store._rows(sql, params)
        store._rows("release savepoint alert_read")
        return rows
    except Exception as exc:  # noqa: BLE001
        try:
            store._rows("rollback to savepoint alert_read")
        except Exception:  # noqa: BLE001
            pass
        log.debug("alert read skipped: %s", sanitize(str(exc)))
        return None


class Alerter:
    """Record, deduplicate and send alerts on the worker's JobStore connection."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 clock=None) -> None:
        self.settings = settings
        self.transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def recipients(self, app_settings: Mapping[str, Any]) -> list[str]:
        listed = app_settings.get("alert_emails")
        if isinstance(listed, list) and listed:
            return [str(x) for x in listed if str(x).strip()]
        return list(self.settings.alert_emails)

    def raise_all(self, store: JobStore, alerts: Sequence[Alert]) -> list[int]:
        """Record each alert (cooldown permitting) and email it. Returns alert ids.
        Runs inside a savepoint so a failure here never undoes the caller's work."""
        if not alerts:
            return []
        ids: list[int] = []
        try:
            app = store.settings()
        except Exception:  # noqa: BLE001
            app = {}
        cooldown = timedelta(minutes=_setting(app, "alert_cooldown_minutes", 360))
        to = self.recipients(app)
        for alert in alerts:
            try:
                store._rows("savepoint alert")
                if self._cooling(store, alert.key, cooldown):
                    store._rows("release savepoint alert")
                    continue
                channel, sent_at, error = self._send(alert, to)
                rows = store._rows(
                    f"insert into {store._t('alerts')} (key, severity, repo, job_id, subject, body, "
                    f"recipients, channel, sent_at, error) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"returning id",
                    [alert.key, alert.severity, alert.repo, alert.job_id, alert.subject,
                     sanitize(alert.body, 8000), to, channel, sent_at, sanitize(error)],
                )
                store._rows("release savepoint alert")
                ids.append(rows[0][0])
                level = logging.ERROR if alert.severity == "critical" else logging.WARNING
                log.log(level, "alert %s: %s (%s)", alert.key, alert.subject,
                        "emailed" if sent_at else f"not emailed: {error or 'no channel'}")
            except Exception as exc:  # noqa: BLE001 - alerting must never break a run
                try:
                    store._rows("rollback to savepoint alert")
                except Exception:  # noqa: BLE001
                    pass
                log.warning("could not record alert %s: %s", alert.key, sanitize(str(exc)))
        return ids

    def last(self, store: JobStore, key: str) -> datetime | None:
        rows = _safe(store, f"select max(at) from {store._t('alerts')} where key = %s", [key])
        return rows[0][0] if rows else None

    def _cooling(self, store: JobStore, key: str, cooldown: timedelta) -> bool:
        last = self.last(store, key)
        return last is not None and last > self._clock() - cooldown

    def _send(self, alert: Alert, to: Sequence[str]) -> tuple[str, datetime | None, str | None]:
        key = self.settings.resend_api_key
        if not to:
            return "none", None, "no recipients (app_settings.alert_emails is empty)"
        if not key:
            return "none", None, "RESEND_API_KEY is not set"
        try:
            with httpx.Client(timeout=15, transport=self.transport) as http:
                r = http.post(RESEND_URL, headers={"Authorization": f"Bearer {key}"}, json={
                    "from": self.settings.alert_from, "to": list(to),
                    "subject": f"[GitScout {alert.severity}] {alert.subject}",
                    "text": alert.body + "\n\n-- GitScout worker",
                })
        except httpx.HTTPError as exc:
            return "email", None, f"send failed: {exc}"
        if r.status_code >= 300:
            return "email", None, f"Resend answered {r.status_code}: {r.text[:300]}"
        return "email", self._clock(), None

    # --- the checks the worker runs ------------------------------------------
    def after_job(self, store: JobStore, job: Job, status: str, *, retry: int | None,
                  error: str | None, counters: Mapping[str, Any], rate: Mapping[str, Any]) -> list[int]:
        try:
            app = store.settings()
        except Exception:  # noqa: BLE001
            app = {}
        alerts = for_finished_job(job, status, retry=retry, error=error, counters=counters,
                                  rate=rate, rate_floor=_setting(app, "rate_alert_floor", 250))
        if status == "succeeded":
            failed_key = f"job_failed:{job.repo}:{job.kind}"
            recovered_key = f"recovered:{job.repo}:{job.kind}"
            failed_at, recovered_at = self.last(store, failed_key), self.last(store, recovered_key)
            if failed_at and (recovered_at is None or recovered_at < failed_at):
                alerts.append(Alert(recovered_key, "info", f"{job.repo}: {job.kind} runs recovered",
                                    f"{job.kind} job {job.id} for {job.repo} succeeded after the failure "
                                    f"reported at {failed_at.isoformat()}.", job.repo, job.id))
        return self.raise_all(store, alerts)

    def check_overdue(self, store: JobStore) -> list[int]:
        hours = _setting(store.settings(), "overdue_hours", 26)
        rows = _safe(
            store,
            f"select repo, schedule_cron, timezone, last_success_at, last_job_id, last_job_status, "
            f"last_job_error from {store._t('repo_health')} where overdue",
        )
        if rows is None:  # before migration 0013 the view is missing
            return []
        names = ("repo", "schedule_cron", "timezone", "last_success_at", "last_job_id",
                 "last_job_status", "last_job_error")
        return self.raise_all(store, [for_overdue(dict(zip(names, r)), hours) for r in rows])
