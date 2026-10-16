"""Transactional email for the dashboard (invitations, password resets) via Resend.

Returns an error string instead of raising: when mail is not configured the dashboard
shows the link to the administrator who asked for it, who passes it on.
"""
from __future__ import annotations

import httpx

from ..alerts import RESEND_URL
from ..config import Settings


def send(settings: Settings, to: str, subject: str, text: str,
         *, transport: httpx.BaseTransport | None = None) -> str | None:
    if not settings.resend_api_key:
        return "email is not configured (RESEND_API_KEY)"
    try:
        with httpx.Client(timeout=15, transport=transport) as http:
            r = http.post(RESEND_URL, headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                          json={"from": settings.alert_from, "to": [to], "subject": subject, "text": text})
    except httpx.HTTPError as exc:
        return f"send failed: {exc}"
    if r.status_code >= 300:
        return f"Resend answered {r.status_code}"
    return None
