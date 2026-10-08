"""Third-party website fetching, with the SSRF guard.

Split out of ``enrich`` so both the REST and GraphQL paths can scan the site a user
links from their profile. This client **never** carries a GitHub token: a profile
``blog`` field is attacker-controlled, so it is treated as hostile input throughout --
private/loopback addresses are refused (on redirects too), responses are size-capped,
and non-text content types are dropped.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx

from .emails import extract_emails_from_text, score
from .models import EmailCandidate

log = logging.getLogger(__name__)

HostCheck = Callable[[str], Awaitable[bool]]
MAX_PAGE_BYTES = 500_000
MAX_REDIRECTS = 3
WEBSITE_PATHS = ("", "/contact", "/about")


async def is_public_host(host: str) -> bool:
    """SSRF guard: only fetch hosts that resolve exclusively to public IP addresses."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def same_site(email: str, host: str) -> bool:
    """Is this address on the domain we found it on? A strong signal it is really theirs."""
    domain = email.rsplit("@", 1)[-1]
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


class WebsiteScanner:
    """Looks for addresses on the site a user links from their GitHub profile."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        host_check: HostCheck = is_public_host,
    ) -> None:
        self.owns_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": "gitscout/0.2"}, follow_redirects=False
        )
        self.host_check = host_check

    async def aclose(self) -> None:
        if self.owns_http:
            await self.http.aclose()

    async def scan(self, blog: str) -> list[EmailCandidate]:
        base = blog if "://" in blog else f"https://{blog}"
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return []
        host = parsed.hostname.lower()
        if host == "github.com" or host.endswith(".github.com"):
            return []

        for path in WEBSITE_PATHS:
            text = await self.fetch_text(urljoin(base, path) if path else base)
            emails = extract_emails_from_text(text) if text else []
            if emails:
                return [
                    EmailCandidate(email, "website", score("website", name_match=same_site(email, host)))
                    for email in emails[:3]
                ]
        return []

    async def fetch_text(self, url: str) -> str | None:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlparse(url).hostname
            if not host or not await self.host_check(host):
                return None
            try:
                async with self.http.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        if urlparse(url).scheme not in ("http", "https"):
                            return None
                        continue
                    if resp.status_code != 200:
                        return None
                    ctype = resp.headers.get("content-type", "")
                    if ctype and "text" not in ctype and "html" not in ctype:
                        return None
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            break
                    return b"".join(chunks).decode("utf-8", errors="replace")
            except (httpx.HTTPError, ValueError):
                return None
        return None
