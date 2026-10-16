"""GitHub App authentication: installation tokens that renew themselves.

A personal access token belongs to a person and expires; when it does, the pipeline
stops until someone makes a new one. A GitHub App belongs to the organisation and
mints a fresh installation token on demand, valid for an hour. This module does the
minting: a short-lived JWT signed with the App's private key, exchanged for an
installation token, cached until it is about to expire.

Only ``cryptography`` is needed (RS256 is a PKCS#1 v1.5 signature over SHA-256); no
JWT library, nothing else.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

import httpx

from .github_client import GitHubAuthError

DEFAULT_API = "https://api.github.com"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    """The App JWT GitHub wants: iss = app id, 10-minute life, issued a minute ago to
    absorb clock skew between here and GitHub."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubAuthError("GITHUB_APP_PRIVATE_KEY is not a readable PEM private key") from exc
    issued = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(
        {"iat": issued - 60, "exp": issued + 9 * 60, "iss": str(app_id)}, separators=(",", ":")
    ).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return f"{header}.{payload}.{_b64(signature)}"


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    installation_id: str
    private_key_pem: str

    @classmethod
    def from_settings(cls, settings) -> "AppCredentials | None":
        if not settings.has_github_app:
            return None
        return cls(
            app_id=str(settings.github_app_id),
            installation_id=str(settings.github_app_installation_id),
            private_key_pem=settings.github_app_private_key,
        )


def _parse_expiry(value: str) -> float:
    # GitHub: "2026-10-16T12:34:56Z"
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


class InstallationTokens:
    """Mints installation tokens and hands out the current one.

    ``current()`` returns a token with at least ``margin`` seconds of life left,
    minting a new one when needed. ``refresh()`` is the shape the GraphQL client's
    ``token_refresh`` hook wants.
    """

    def __init__(
        self,
        creds: AppCredentials,
        *,
        api: str = DEFAULT_API,
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        margin: float = 10 * 60,
    ) -> None:
        self.creds = creds
        self._api = api.rstrip("/")
        self._http = http or httpx.AsyncClient(transport=transport, timeout=30.0)
        self._clock = clock
        self._margin = margin
        self._token: str | None = None
        self._expires_at: float = 0.0
        self.minted = 0

    @property
    def seconds_left(self) -> float:
        return self._expires_at - self._clock()

    async def current(self) -> str:
        if self._token is None or self.seconds_left < self._margin:
            await self._mint()
        assert self._token is not None
        return self._token

    async def refresh(self) -> list[str]:
        """Always mints: what the client calls when it decides the token is stale."""
        await self._mint()
        return [self._token or ""]

    async def _mint(self) -> None:
        jwt = app_jwt(self.creds.app_id, self.creds.private_key_pem, self._clock())
        url = f"{self._api}/app/installations/{self.creds.installation_id}/access_tokens"
        try:
            resp = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "gitscout/0.2",
                },
            )
        except httpx.TransportError as exc:
            raise GitHubAuthError(f"could not reach GitHub to mint an installation token: {exc}") from exc
        if resp.status_code != 201:
            detail = ""
            try:
                detail = (resp.json() or {}).get("message", "")
            except ValueError:
                pass
            raise GitHubAuthError(
                f"GitHub refused to mint an installation token ({resp.status_code}): "
                f"{detail or 'no detail'}. Check GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID "
                f"and that the App is installed on the organisation."
            )
        body: Mapping[str, object] = resp.json()
        self._token = str(body["token"])
        self._expires_at = _parse_expiry(str(body["expires_at"]))
        self.minted += 1

    async def aclose(self) -> None:
        await self._http.aclose()
