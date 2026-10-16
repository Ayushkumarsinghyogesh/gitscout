"""Supabase Auth, server side only.

The browser never talks to Supabase: it posts a form to the dashboard, the dashboard
calls Auth with the project's secret key, and the resulting tokens live in a signed,
HTTP-only cookie. Nobody can sign up; accounts exist because an administrator invited
them (``generate_link`` type ``invite``), and the role that matters is the row in
``gitscout.app_users``, which only the server writes.

Invite and password-reset emails carry a link to the dashboard itself
(``/auth/confirm?token_hash=..&type=..``), not to Supabase. The page behind it asks
for one click before the token is spent, so a mail scanner that opens every link
cannot use up the invitation.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class AuthError(Exception):
    """Auth refused the request (bad password, expired link, unknown user)."""


@dataclass
class Session:
    access_token: str
    refresh_token: str
    expires_at: int
    user_id: str
    email: str


class AuthProvider(Protocol):
    def sign_in(self, email: str, password: str) -> Session: ...
    def refresh(self, refresh_token: str) -> Session: ...
    def user(self, access_token: str) -> dict[str, Any]: ...
    def set_password(self, access_token: str, password: str) -> None: ...
    def verify(self, token_hash: str, kind: str) -> Session: ...
    def generate_link(self, kind: str, email: str) -> tuple[str, str, str]: ...
    def sign_out(self, access_token: str) -> None: ...


def _session(body: dict[str, Any]) -> Session:
    user = body.get("user") or {}
    if not body.get("access_token") or not user.get("id"):
        raise AuthError("Auth returned no session")
    expires_at = int(body.get("expires_at") or (time.time() + int(body.get("expires_in") or 3600)))
    return Session(body["access_token"], body.get("refresh_token") or "", expires_at,
                   user["id"], (user.get("email") or "").lower())


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    return str(body.get("msg") or body.get("error_description") or body.get("message")
               or body.get("error") or f"HTTP {r.status_code}")


class SupabaseAuth:
    def __init__(self, url: str, key: str, *, transport: httpx.BaseTransport | None = None,
                 cache_seconds: int = 60) -> None:
        self.base = url.rstrip("/") + "/auth/v1"
        self.key = key
        self.http = httpx.Client(timeout=20, transport=transport, headers={"apikey": key})
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.cache_seconds = cache_seconds

    def _post(self, path: str, json: dict[str, Any], *, bearer: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        r = self.http.post(self.base + path, json=json, headers=headers)
        if r.status_code >= 300:
            raise AuthError(_message(r))
        return r.json() if r.content else {}

    def sign_in(self, email: str, password: str) -> Session:
        return _session(self._post("/token?grant_type=password", {"email": email, "password": password}))

    def refresh(self, refresh_token: str) -> Session:
        return _session(self._post("/token?grant_type=refresh_token", {"refresh_token": refresh_token}))

    def user(self, access_token: str) -> dict[str, Any]:
        """Validate a token by asking Auth who it belongs to (cached briefly)."""
        key = hashlib.sha256(access_token.encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        r = self.http.get(self.base + "/user", headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code >= 300:
            raise AuthError(_message(r))
        user = r.json()
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = (time.monotonic() + self.cache_seconds, user)
        return user

    def set_password(self, access_token: str, password: str) -> None:
        r = self.http.put(self.base + "/user", json={"password": password},
                          headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code >= 300:
            raise AuthError(_message(r))

    def verify(self, token_hash: str, kind: str) -> Session:
        return _session(self._post("/verify", {"type": kind, "token_hash": token_hash}))

    def generate_link(self, kind: str, email: str) -> tuple[str, str, str]:
        """(user id, hashed token, link type) for an invite or recovery link. An invite
        for an address that already has an account becomes a recovery (set a password)."""
        try:
            body = self._post("/admin/generate_link", {"type": kind, "email": email}, bearer=self.key)
        except AuthError as exc:
            if kind == "invite" and "already" in str(exc).lower():
                body = self._post("/admin/generate_link", {"type": "recovery", "email": email}, bearer=self.key)
                kind = "recovery"
            else:
                raise
        props = body.get("properties") or body
        user = body.get("user") or body
        if not props.get("hashed_token") or not user.get("id"):
            raise AuthError("Auth returned no link")
        return user["id"], props["hashed_token"], kind

    def sign_out(self, access_token: str) -> None:
        try:
            self._post("/logout", {}, bearer=access_token)
        except (AuthError, httpx.HTTPError):
            pass  # the cookie is dropped either way
