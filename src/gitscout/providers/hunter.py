"""Hunter.io email-finder provider (optional, requires HUNTER_API_KEY).

Hunter needs a *domain* plus a name. We take the domain from the user's company or
their linked website -- so this only fires for people who have told GitHub where they
work, which is also the population where a guess is most likely to be right.

Hunter returns its own 0-100 confidence; we rescale it into gitscout's 0-1 range and
cap it below the GitHub-sourced scores, because a derived address is never as good as
one the person actually published or committed with.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from ..emails import normalize_email
from ..models import EmailCandidate, Profile

log = logging.getLogger(__name__)

API_URL = "https://api.hunter.io/v2/email-finder"
#: Never let a derived address outrank a committed or published one.
MAX_CONFIDENCE = 0.70
MIN_CONFIDENCE = 0.35

#: Free-mail hosts: Hunter cannot do anything useful with these as a "company domain".
_PUBLIC_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
    "qq.com",
    "163.com",
}
#: Hosting platforms people link to; the domain is not their employer.
_PLATFORM_DOMAINS = {
    "github.com",
    "github.io",
    "gitlab.com",
    "medium.com",
    "dev.to",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "notion.site",
    "substack.com",
    "vercel.app",
    "netlify.app",
    "herokuapp.com",
    "wordpress.com",
    "blogspot.com",
}

_CLEAN_COMPANY = re.compile(r"[^A-Za-z0-9 .&-]")


def domain_from_profile(profile: Profile) -> str | None:
    """Best guess at the person's employer domain, or None if we should not ask."""
    blog = (profile.blog or "").strip()
    if blog:
        url = blog if "://" in blog else f"https://{blog}"
        host = (urlparse(url).hostname or "").lower().lstrip(".")
        host = host[4:] if host.startswith("www.") else host
        if host and "." in host:
            registrable = ".".join(host.split(".")[-2:])
            if registrable not in _PUBLIC_DOMAINS and not any(
                host == p or host.endswith("." + p) for p in _PLATFORM_DOMAINS
            ):
                return host
    return None


def split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) < 2:
        return (parts[0], None) if parts else (None, None)
    return parts[0], parts[-1]


def confidence_from(raw: Any) -> float:
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return MIN_CONFIDENCE
    return round(max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, pct / 100.0 * MAX_CONFIDENCE)), 2)


class HunterProvider:
    name = "hunter"

    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "gitscout/0.2"}
        )
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def find(self, profile: Profile) -> list[EmailCandidate]:
        first, last = split_name(profile.name)
        domain = domain_from_profile(profile)
        if not domain or not first or not last:
            return []  # not enough to ask with; do not waste a credit

        self.calls += 1
        resp = await self._http.get(
            API_URL,
            params={
                "domain": domain,
                "first_name": first,
                "last_name": last,
                "api_key": self._api_key,
            },
        )
        if resp.status_code in (401, 403):
            log.warning("Hunter rejected the API key")
            return []
        if resp.status_code == 429:
            log.warning("Hunter rate limit reached")
            return []
        if resp.status_code >= 400:
            log.debug("Hunter %s for %s", resp.status_code, profile.login)
            return []

        data = (resp.json() or {}).get("data") or {}
        email = normalize_email(data.get("email"))
        if not email:
            return []
        return [EmailCandidate(email, "hunter", confidence_from(data.get("score")))]
