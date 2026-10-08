"""Stage 2 over GraphQL: commit author emails, batched.

The public profile email is already captured during ingest, for free. This module
handles everyone it missed -- the majority -- by reading the author lines of commits
in each user's own public repos.

Why this is cheap: one aliased query probes ~10 users at once (3 repos x 5 commits
each), which is ~40 connection requests, i.e. **~1 point for 10 users**. The REST
ladder spends ~4 requests *per user*.

Why the commits are not server-side filtered by author: ``history(author: {id: ...})``
only matches commits GitHub has already linked to the account, which excludes exactly
the unlinked commits whose addresses we most want. We over-fetch slightly and attribute
client-side: an exact login match scores full confidence, a plausible name match scores
half, and anything else is dropped.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .emails import name_matches, normalize_email, score
from .graphql import GraphQLClient, NodeLimitExceeded, QueryResult
from .models import EmailCandidate, Profile
from .queries import alias_for, commit_email_query
from .storage import Store
from .web import WebsiteScanner

log = logging.getLogger(__name__)

#: How many commit emails to keep per user. More than two is almost always noise.
MAX_PER_USER = 2


@dataclass
class EnrichGqlSummary:
    processed: int = 0
    with_email: int = 0
    emails_new: int = 0
    points: int = 0
    queries: int = 0
    failed: int = 0
    websites_scanned: int = 0


def commit_candidates(profile: Profile, probe: Mapping[str, Any] | None) -> list[EmailCandidate]:
    """Turn one user's CommitProbe result into scored email candidates.

    Attribution rules, in order:
      * ``author.user.login`` equals the account  -> full confidence
      * the git author *name* plausibly matches   -> half confidence
      * otherwise                                 -> dropped (likely a co-author or
        a commit merged from someone else)
    """
    if not probe:
        return []
    login = probe.get("login") or profile.login
    profile_name = probe.get("name") or profile.name

    strong: Counter[str] = Counter()
    weak: Counter[str] = Counter()

    repos = (probe.get("repositories") or {}).get("nodes") or []
    for repo in repos:
        if not repo:
            continue
        target = ((repo.get("defaultBranchRef") or {}).get("target")) or {}
        history = (target.get("history") or {}).get("nodes") or []
        for commit in history:
            if not commit:
                continue
            author = commit.get("author") or {}
            email = normalize_email(author.get("email"))
            if not email:
                continue
            linked = ((author.get("user") or {}).get("login") or "").lower()
            if linked and linked == login.lower():
                strong[email] += 1
            elif not linked and name_matches(login, profile_name, author.get("name")):
                weak[email] += 1

    out: list[EmailCandidate] = []
    for email, _ in strong.most_common(MAX_PER_USER):
        out.append(EmailCandidate(email, "gql_commit", score("commit_api", name_match=True)))
    if len(out) < MAX_PER_USER:
        known = {c.email for c in out}
        for email, _ in weak.most_common(MAX_PER_USER - len(out)):
            if email not in known:
                out.append(
                    EmailCandidate(email, "gql_commit", score("commit_api", name_match=False))
                )
    return out


class GqlEnricher:
    """Finds commit emails for users the profile pass did not cover."""

    def __init__(
        self,
        client: GraphQLClient,
        store: Store,
        *,
        batch_size: int = 10,
        repos_per_user: int = 3,
        commits_per_repo: int = 5,
        scan_websites: bool = False,
        scanner: WebsiteScanner | None = None,
        website_concurrency: int = 8,
    ) -> None:
        self.client = client
        self.store = store
        self.batch_size = max(1, min(25, batch_size))
        self.repos_per_user = max(1, repos_per_user)
        self.commits_per_repo = max(1, commits_per_repo)
        self.scan_websites = scan_websites
        self.website_concurrency = max(1, website_concurrency)
        self._owns_scanner = scanner is None and scan_websites
        self._scanner = scanner or (WebsiteScanner() if scan_websites else None)

    async def __aenter__(self) -> "GqlEnricher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_scanner and self._scanner is not None:
            await self._scanner.aclose()

    async def run(
        self,
        *,
        limit: int | None = None,
        refresh: bool = False,
        progress: Any = None,
    ) -> EnrichGqlSummary:
        logins = self.store.logins_without_email(limit=limit, refresh=refresh)
        summary = EnrichGqlSummary()
        if not logins:
            return summary

        total = len(logins)
        for start in range(0, total, self.batch_size):
            batch = logins[start : start + self.batch_size]
            try:
                await self._probe_batch(batch, summary)
            except NodeLimitExceeded:
                # Too much for one query: fall back to probing each user alone.
                log.warning("Node limit on a batch of %d; retrying one at a time", len(batch))
                for login in batch:
                    try:
                        await self._probe_batch([login], summary)
                    except Exception as exc:  # noqa: BLE001 - one bad user must not stop the run
                        summary.failed += 1
                        log.warning("commit probe failed for %s: %s", login, exc)
            if progress:
                progress(min(start + self.batch_size, total), total)

        if self.scan_websites:
            await self._scan_websites(summary, limit=limit)
        return summary

    async def _probe_batch(self, logins: Sequence[str], summary: EnrichGqlSummary) -> None:
        query, variables = commit_email_query(logins)
        variables.update({"repos": self.repos_per_user, "commits": self.commits_per_repo})
        result: QueryResult = await self.client.execute(query, variables)
        summary.points += result.cost
        summary.queries += 1

        profiles = self.store.profiles(logins)
        for index, login in enumerate(logins):
            probe = result.data.get(alias_for(index))
            profile = profiles.get(login) or Profile(login=login)
            candidates = commit_candidates(profile, probe)
            if candidates:
                summary.emails_new += self.store.add_emails(login, candidates)
                summary.with_email += 1
            summary.processed += 1

        # Mark the whole batch probed so a re-run moves on, even for users with no hit.
        self.store.mark_probed(logins, "commit_probed_at")
        self.store.mark_probed(logins, "discovered_at")

    async def _scan_websites(self, summary: EnrichGqlSummary, *, limit: int | None) -> None:
        """Last resort: the site a user links from their profile."""
        if self._scanner is None:
            return
        logins = self.store.logins_without_email(limit=limit, column="provider_probed_at")
        profiles = self.store.profiles(logins)
        targets = [(lg, p.blog) for lg, p in profiles.items() if p.blog]
        if not targets:
            return

        sem = asyncio.Semaphore(self.website_concurrency)

        async def one(login: str, blog: str) -> None:
            async with sem:
                try:
                    candidates = await self._scanner.scan(blog)  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001 - a bad site must not stop the run
                    log.debug("website scan failed for %s: %s", login, exc)
                    return
                summary.websites_scanned += 1
                if candidates:
                    summary.emails_new += self.store.add_emails(login, candidates)
                    summary.with_email += 1

        await asyncio.gather(*(one(login, blog) for login, blog in targets))
        self.store.mark_probed([lg for lg, _ in targets], "provider_probed_at")
