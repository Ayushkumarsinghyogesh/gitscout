"""GitHub App auth: the JWT is one GitHub accepts, tokens are minted and renewed."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from gitscout.config import Settings
from gitscout.github_app import AppCredentials, InstallationTokens, app_jwt
from gitscout.github_client import GitHubAuthError

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key, pem


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_app_jwt_is_rs256_with_the_claims_github_requires(keypair):
    key, pem = keypair
    token = app_jwt("12345", pem, now=1_800_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_unb64(payload))
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "12345"}
    key.public_key().verify(_unb64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_a_bad_key_is_a_clear_error(keypair):
    with pytest.raises(GitHubAuthError, match="PEM"):
        app_jwt("1", "not a key")


def test_credentials_come_from_settings_only_when_complete(keypair):
    _, pem = keypair
    assert AppCredentials.from_settings(Settings(tokens=("t",))) is None
    assert AppCredentials.from_settings(Settings(github_app_id="1", github_app_installation_id="2")) is None
    creds = AppCredentials.from_settings(
        Settings(github_app_id="1", github_app_installation_id="2", github_app_private_key=pem)
    )
    assert creds == AppCredentials("1", "2", pem)


def _github(responses: list[tuple[int, dict]], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_tokens_are_minted_once_and_renewed_near_expiry(keypair):
    _, pem = keypair
    now = {"t": 1_800_000_000.0}
    seen: list[httpx.Request] = []
    transport = _github(
        [(201, {"token": "ghs_first", "expires_at": "2027-01-15T09:00:00Z"}),
         (201, {"token": "ghs_second", "expires_at": "2027-01-15T10:00:00Z"}),
         (201, {"token": "ghs_third", "expires_at": "2027-01-15T11:00:00Z"})],
        seen,
    )
    tokens = InstallationTokens(AppCredentials("7", "99", pem), transport=transport, clock=lambda: now["t"])
    # now is 2027-01-15T08:00:00Z; the first token expires an hour later, at 1_800_003_600
    assert asyncio.run(tokens.current()) == "ghs_first"
    assert asyncio.run(tokens.current()) == "ghs_first"            # cached
    assert tokens.minted == 1 and seen[0].url.path == "/app/installations/99/access_tokens"
    assert seen[0].headers["authorization"].startswith("Bearer ey")
    now["t"] = 1_800_003_600 - 5 * 60                               # 5 min left: under the 10-min margin
    assert asyncio.run(tokens.current()) == "ghs_second" and tokens.minted == 2
    assert asyncio.run(tokens.refresh()) == ["ghs_third"] and tokens.minted == 3   # refresh always mints


def test_github_refusal_is_reported_without_the_jwt(keypair):
    _, pem = keypair
    transport = _github([(401, {"message": "A JSON web token could not be decoded"})], [])
    tokens = InstallationTokens(AppCredentials("7", "99", pem), transport=transport)
    with pytest.raises(GitHubAuthError) as exc:
        asyncio.run(tokens.current())
    assert "401" in str(exc.value) and "could not be decoded" in str(exc.value) and "ey" not in str(exc.value)[:40]
