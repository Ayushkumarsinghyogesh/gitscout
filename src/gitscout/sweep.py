"""The deletion sweep: find what GitHub no longer lists, mark it, never delete it.

An incremental crawl walks threads by UPDATED_AT, so it sees edits and new comments.
It cannot see an absence: an issue that was deleted, converted, or transferred to
another repository simply stops appearing. The sweep lists every thread number that
still exists (one point per hundred, so a repository the size of trivy costs about
110 points) and compares that with what is stored.

* stored but not listed  -> ``source_deleted_at`` is set; the row, its comments and
  its history stay, and the outreach dataset leaves the thread out
* listed and marked      -> the mark is removed (restored or transferred back)

The listing must be *complete* before anything is marked. A sweep that stops early
(network, rate limit) reports ``complete: false`` for that kind and marks nothing,
because a partial listing would make everything after the cut look deleted.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from .graphql import GraphQLClient
from .queries import THREAD_NUMBERS
from .storage import Store
from .targets import parse_repo

log = logging.getLogger(__name__)


async def live_numbers(client: GraphQLClient, repo: str, kind: str) -> tuple[set[int], bool, int | None]:
    """Every thread number GitHub lists for this kind. Returns (numbers, complete,
    totalCount). `complete` is False when the walk ended before the last page."""
    query, path = THREAD_NUMBERS[kind]
    owner, name = parse_repo(repo).split("/", 1)
    numbers: set[int] = set()
    complete = False
    total: int | None = None
    async for page in client.paginate(query, {"owner": owner, "name": name}, path):
        total = page.connection.get("totalCount") if isinstance(page.connection, Mapping) else total
        for node in page.nodes:
            try:
                numbers.add(int(node["number"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not page.has_next:
            complete = True
    # A repository with zero threads of this kind: the first page says so.
    if total == 0:
        complete = True
    return numbers, complete, total


async def sweep_repo(
    client: GraphQLClient,
    store: Store,
    repo: str,
    kinds: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Mark and restore for each kind; returns the per-kind report the job records."""
    repo = parse_repo(repo)
    report: dict[str, dict[str, Any]] = {}
    for kind in kinds:
        if kind not in THREAD_NUMBERS:
            continue
        before = client.points_used
        try:
            live, complete, total = await live_numbers(client, repo, kind)
        except Exception as exc:  # noqa: BLE001 - one kind failing must not stop the others
            log.warning("sweep %s %s: listing failed: %s", repo, kind, exc)
            report[kind] = {"complete": False, "error": str(exc)[:300], "marked": 0, "restored": 0}
            continue
        entry: dict[str, Any] = {
            "complete": complete, "live": len(live), "total_count": total,
            "stored": 0, "marked": 0, "restored": 0, "points": client.points_used - before,
        }
        stored = store.thread_numbers(repo, kind)
        entry["stored"] = len(stored)
        if complete:
            missing = stored - live
            entry["marked"] = store.mark_threads_deleted(repo, kind, missing)
            back = store.deleted_thread_numbers(repo, kind) & live
            entry["restored"] = store.restore_threads(repo, kind, back)
            if entry["marked"] or entry["restored"]:
                log.info("sweep %s %s: %d marked deleted, %d restored", repo, kind, entry["marked"], entry["restored"])
        else:
            log.warning("sweep %s %s: listing incomplete (%d of %s); nothing marked",
                        repo, kind, len(live), total)
        report[kind] = entry
    return report
