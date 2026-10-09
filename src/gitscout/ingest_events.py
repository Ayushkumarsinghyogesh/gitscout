"""Stars via the repo events API — the only route left to stargazers.

GitHub [restricted the stargazer list to repo admins on 2026-06-30][1], so
``repository.stargazers`` returns an empty connection and REST ``/stargazers`` 404s for
any repo you do not administer. The public star *count* still works, the list does not.

The events timeline is the way around it: ``GET /repos/{owner}/{repo}/events`` still
carries ``WatchEvent``, which is GitHub's name for "someone starred this", complete with
``actor.login`` and ``created_at``.

**Know the limit before relying on this.** The events endpoint caps at **300 events
(3 pages)** and roughly 90 days, whichever comes first. On a busy repo that window is
short — about a day and a half for Prowler — so:

* you **cannot** backfill a repo's historical stargazers; they are gone for good
* you **can** capture every new star from now on, provided you poll more often than the
  window turns over. A 6-hourly cron is comfortable for even a very active repo.

The events API gives only a login, so profiles are then fetched in batches over GraphQL
(~1 point per 25 people) rather than one REST call each.

[1]: https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from .github_client import GitHubClient
from .graphql import GraphQLClient, MissingScopeError
from .ingest import parse_repo
from .ingest_gql import EmailScope, GqlIngestResult, _Item, _record_profiles, _user_type, profile_from_node
from .models import Interaction
from .queries import alias_for, profiles_query
from .storage import Store

log = logging.getLogger(__name__)

#: GitHub's event type for a star. (It is called "watch" for historical reasons; real
#: watching/subscribing is not in the events timeline at all.)
STAR_EVENT = "WatchEvent"

#: Hard ceiling in the API: 100 per page, 3 pages.
MAX_EVENT_PAGES = 3
EVENTS_PER_PAGE = 100

#: Profiles looked up per GraphQL query.
PROFILE_BATCH = 25

SKIP_LOGINS = {"ghost"}


@dataclass
class StarEvent:
    login: str
    starred_at: str


async def star_events(
    client: GitHubClient,
    repo: str,
    *,
    stop_at: str | None = None,
    max_pages: int = MAX_EVENT_PAGES,
) -> tuple[list[StarEvent], bool]:
    """Recent star events, newest first. Returns (events, stopped_early).

    ``stop_at`` is a high-water mark: paging halts at the first event at or older than
    it, which is what makes a scheduled run cheap.
    """
    found: list[StarEvent] = []
    seen: set[str] = set()
    stopped_early = False

    for page in range(1, max_pages + 1):
        data = await client.get_json(
            f"/repos/{repo}/events", {"per_page": EVENTS_PER_PAGE, "page": page}
        )
        if not data:
            break
        if not isinstance(data, list):
            # The endpoint answers with an object for errors and for the "pagination is
            # limited" notice; neither is a page of events.
            log.debug("%s events page %d was not a list; stopping", repo, page)
            break

        for event in data:
            if not isinstance(event, dict) or event.get("type") != STAR_EVENT:
                continue
            created = event.get("created_at") or ""
            if stop_at and created and created <= stop_at:
                stopped_early = True
                break
            login = ((event.get("actor") or {}).get("login") or "").strip()
            if not login or login in SKIP_LOGINS or login in seen:
                continue
            seen.add(login)
            found.append(StarEvent(login, created))

        if stopped_early or len(data) < EVENTS_PER_PAGE:
            break

    return found, stopped_early


async def _fetch_profiles(
    gql: GraphQLClient,
    logins: Sequence[str],
    scope: EmailScope,
    result: GqlIngestResult,
) -> dict[str, dict]:
    """Batch-look-up profiles for star event actors."""
    out: dict[str, dict] = {}
    for start in range(0, len(logins), PROFILE_BATCH):
        batch = logins[start : start + PROFILE_BATCH]
        query, variables = profiles_query(batch, scope.enabled)
        try:
            res = await gql.execute(query, variables)
        except MissingScopeError:
            if not scope.enabled:
                raise
            scope.downgrade()
            query, variables = profiles_query(batch, include_email=False)
            res = await gql.execute(query, variables)
        result.points += res.cost
        for index, login in enumerate(batch):
            node = res.data.get(alias_for(index))
            if node:
                out[login] = node
    return out


async def ingest_stars_via_events(
    rest: GitHubClient,
    gql: GraphQLClient,
    store: Store,
    repo: str,
    *,
    incremental: bool = False,
    fresh: bool = False,
    max_items: int = 0,
    scope: EmailScope | None = None,
) -> GqlIngestResult:
    """Collect recent stargazers from the events timeline, with full profiles."""
    repo = parse_repo(repo)
    scope = scope if scope is not None else EmailScope()
    key = f"events:{repo}:stars"
    result = GqlIngestResult(repo, "stars")

    if fresh:
        store.clear_crawl_state(key)
    state = store.get_crawl_state(key) or {}
    high_water = state.get("high_water") if incremental else None

    events, stopped_early = await star_events(rest, repo, stop_at=high_water)
    result.stopped_early = stopped_early
    if max_items:
        events = events[:max_items]

    if not events:
        log.info("%s: no new star events in the API's window", key)
        result.complete = True
        if high_water:
            store.set_crawl_state(key, total_count=state.get("total_count"))
        return result

    result.pages = 1
    nodes = await _fetch_profiles(gql, [e.login for e in events], scope, result)

    items: list[_Item] = []
    for event in events:
        node = nodes.get(event.login)
        # A null node means GitHub has no such user any more (deleted or renamed).
        # The star still happened, so the interaction is recorded -- but inventing a
        # profile row for them would claim we found a profile we never saw.
        items.append(
            _Item(
                Interaction(
                    repo,
                    event.login,
                    "stars",
                    _user_type((node or {}).get("__typename"), event.login),
                    event.starred_at,
                ),
                profile_from_node(node) if node else None,
            )
        )

    result.fetched = len(items)
    result.new = store.add_interactions([i.interaction for i in items])
    result.profiles, result.emails_new = _record_profiles(store, items)
    result.complete = True

    newest = max((e.starred_at for e in events if e.starred_at), default=None)
    if newest:
        store.set_crawl_state(key, high_water=newest)

    log.info(
        "%s: %d star events via the events API (+%d new, %d profiles, %d points)",
        key,
        result.fetched,
        result.new,
        result.profiles,
        result.points,
    )
    return result
