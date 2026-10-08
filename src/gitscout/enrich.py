"""Stages 2+3: fetch each user's profile and discover their public email.

Discovery ladder (cheapest first; stops at the first hit unless deep=True):
  1. profile          - email the user published on their GitHub profile
  2. events           - author emails on their recent public push events
  3. commit_api       - author emails of their own commits in their own (non-fork) repos
  4. website          - mailto:/emails on the site linked from their profile (opt-in)
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .emails import name_matches, normalize_email, score
from .github_client import GitHubAuthError, GitHubClient, GitHubError
from .models import EmailCandidate, Profile
from .storage import Store
from .web import MAX_PAGE_BYTES, MAX_REDIRECTS, WEBSITE_PATHS, HostCheck, WebsiteScanner, is_public_host, same_site

log = logging.getLogger(__name__)

__all__ = [
    "Enricher",
    "EnrichSummary",
    "HostCheck",
    "is_public_host",
    "profile_from_json",
    "MAX_PAGE_BYTES",
    "MAX_REDIRECTS",
    "WEBSITE_PATHS",
]


def profile_from_json(login: str, data: dict[str, Any] | None) -> Profile:
    if data is None:
        return Profile(login=login, type="Missing", found=False)
    return Profile(
        login=data.get("login") or login,
        type=data.get("type"),
        name=data.get("name"),
        company=data.get("company"),
        bio=data.get("bio"),
        location=data.get("location"),
        blog=(data.get("blog") or None),
        twitter=data.get("twitter_username"),
        public_email=data.get("email"),
        hireable=data.get("hireable"),
        followers=data.get("followers"),
    )


@dataclass
class EnrichSummary:
    processed: int = 0
    with_email: int = 0
    skipped_bots: int = 0
    failed: int = 0


class Enricher:
    def __init__(
        self,
        client: GitHubClient,
        store: Store,
        *,
        deep: bool = False,
        scan_websites: bool = False,
        max_repos: int = 3,
        concurrency: int = 8,
        http: httpx.AsyncClient | None = None,
        host_check: HostCheck = is_public_host,
    ) -> None:
        self.client = client
        self.store = store
        self.deep = deep
        self.scan_websites = scan_websites
        self.max_repos = max_repos
        self.concurrency = max(1, concurrency)
        # Separate client for third-party sites: it must never carry the GitHub token.
        self._scanner = WebsiteScanner(http, host_check=host_check)

    async def __aenter__(self) -> "Enricher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._scanner.aclose()

    # ------------------------------------------------------------------- run

    async def run(
        self,
        *,
        limit: int | None = None,
        refresh: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> EnrichSummary:
        logins = self.store.logins_to_enrich(limit=limit, refresh=refresh)
        summary = EnrichSummary()
        sem = asyncio.Semaphore(self.concurrency)
        total = len(logins)

        async def worker(login: str) -> None:
            async with sem:
                try:
                    found = await self.enrich_user(login)
                    summary.processed += 1
                    summary.with_email += int(found)
                except GitHubAuthError:
                    raise
                except Exception as exc:  # keep going; the user stays un-discovered => retried
                    summary.failed += 1
                    log.warning("enrich failed for %s: %s", login, exc)
                if progress:
                    progress(summary.processed + summary.failed, total)

        results = await asyncio.gather(*(worker(login) for login in logins), return_exceptions=True)
        for res in results:
            if isinstance(res, GitHubAuthError):
                raise res
        return summary

    async def enrich_user(self, login: str) -> bool:
        """Fetch profile + discover emails. Returns True if at least one email was found."""
        data = await self.client.get_json(f"/users/{login}")
        profile = profile_from_json(login, data)
        self.store.upsert_profile(profile)

        if not profile.found or profile.type == "Bot":
            self.store.mark_discovered(login)
            return False

        candidates = await self.discover(profile)
        self.store.add_emails(login, candidates)
        self.store.mark_discovered(login)
        return bool(candidates)

    # ------------------------------------------------------------- discovery

    async def discover(self, profile: Profile) -> list[EmailCandidate]:
        found: dict[str, EmailCandidate] = {}

        def add(cands: list[EmailCandidate]) -> None:
            for c in cands:
                if c.email not in found or c.confidence > found[c.email].confidence:
                    found[c.email] = c

        def done() -> bool:
            return bool(found) and not self.deep

        email = normalize_email(profile.public_email)
        if email:
            add([EmailCandidate(email, "profile", score("profile"))])
        if done():
            return list(found.values())

        add(await self._from_events(profile))
        if done():
            return list(found.values())

        add(await self._from_repo_commits(profile))
        if done():
            return list(found.values())

        if self.scan_websites and profile.blog:
            add(await self._from_website(profile.blog))
        return list(found.values())

    async def _from_events(self, profile: Profile) -> list[EmailCandidate]:
        login = profile.login
        events = await self.client.get_json(f"/users/{login}/events/public", {"per_page": 100})
        counts: Counter[str] = Counter()
        names: dict[str, str | None] = {}
        head_commits: list[tuple[str, str]] = []

        for ev in events or []:
            if ev.get("type") != "PushEvent":
                continue
            payload = ev.get("payload") or {}
            commits = payload.get("commits")
            if commits:
                for c in commits:
                    author = c.get("author") or {}
                    email = normalize_email(author.get("email"))
                    if email:
                        counts[email] += 1
                        names.setdefault(email, author.get("name"))
            elif payload.get("head") and (ev.get("repo") or {}).get("name"):
                # Payload carries no commit list: fall back to looking up the head commit.
                head_commits.append((ev["repo"]["name"], payload["head"]))

        if not counts:
            for repo_name, sha in head_commits[:3]:
                data = await self.client.get_json(f"/repos/{repo_name}/commits/{sha}")
                if not data:
                    continue
                if ((data.get("author") or {}).get("login") or "").lower() != login.lower():
                    continue  # only attribute commits actually authored by this account
                author = (data.get("commit") or {}).get("author") or {}
                email = normalize_email(author.get("email"))
                if email:
                    counts[email] += 1
                    names.setdefault(email, author.get("name"))

        return [
            EmailCandidate(
                email,
                "events",
                score("events", name_match=name_matches(login, profile.name, names.get(email))),
            )
            for email, _ in counts.most_common(2)
        ]

    async def _from_repo_commits(self, profile: Profile) -> list[EmailCandidate]:
        login = profile.login
        repos = await self.client.get_json(
            f"/users/{login}/repos", {"type": "owner", "sort": "pushed", "per_page": 10}
        )
        own = [r for r in (repos or []) if not r.get("fork") and r.get("full_name")]
        counts: Counter[str] = Counter()
        for repo in own[: self.max_repos]:
            commits = await self.client.get_json(
                f"/repos/{repo['full_name']}/commits", {"author": login, "per_page": 5}
            )
            for c in commits or []:
                author = (c.get("commit") or {}).get("author") or {}
                email = normalize_email(author.get("email"))
                if email:
                    counts[email] += 1
        return [
            EmailCandidate(email, "commit_api", score("commit_api"))
            for email, _ in counts.most_common(2)
        ]

    # --------------------------------------------------------------- website

    async def _from_website(self, blog: str) -> list[EmailCandidate]:
        return await self._scanner.scan(blog)

    async def _fetch_text(self, url: str) -> str | None:
        return await self._scanner.fetch_text(url)


_same_site = same_site  # kept as a module-level name for backwards compatibility
