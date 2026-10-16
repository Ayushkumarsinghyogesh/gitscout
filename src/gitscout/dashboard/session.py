"""The login cookie: Auth tokens plus a CSRF token, signed so it cannot be forged.

Signed, not encrypted: the only party who can read it is the browser that holds it,
and it is HTTP-only, so page scripts cannot. Every POST form carries the CSRF token
from the cookie and is refused without it.
"""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from .auth import Session

COOKIE = "gs_session"
MAX_AGE = 7 * 24 * 3600


@dataclass
class Cookie:
    access_token: str
    refresh_token: str
    expires_at: int
    user_id: str
    email: str
    csrf: str

    @classmethod
    def from_session(cls, s: Session, csrf: str | None = None) -> "Cookie":
        return cls(s.access_token, s.refresh_token, s.expires_at, s.user_id, s.email,
                   csrf or secrets.token_urlsafe(24))


class CookieCodec:
    def __init__(self, secret: str) -> None:
        if not secret or len(secret) < 32:
            raise ValueError("GITSCOUT_SESSION_SECRET must be at least 32 characters")
        self._s = URLSafeTimedSerializer(secret, salt="gitscout-session")

    def dumps(self, cookie: Cookie) -> str:
        return self._s.dumps(asdict(cookie))

    def loads(self, raw: str | None) -> Cookie | None:
        if not raw:
            return None
        try:
            data: dict[str, Any] = self._s.loads(raw, max_age=MAX_AGE)
            return Cookie(**data)
        except (BadSignature, TypeError, ValueError):
            return None
