"""Full records for the discussion-shaped sections of a repo: issues, PRs, discussions.

The interaction crawl records *that* someone filed an issue -- `interactions` is keyed
(repo, login, kind), so one row says "alice files issues on prowler" and that is all it
can say. This stage records *which* issues, pull requests and discussions, what was
written in them, and who replied.

Four things shape the design:

* **It is a separate stage, not extra fields on the ingest query.** The interaction
  crawl costs ~1 point per 100 items and the cron path depends on that staying true.
  A page here also pulls the body, labels, participants and a slice of the thread, so
  `gitscout scout` is unchanged unless you ask for `--with-threads`.
* **Incremental runs order by UPDATED_AT, backfills by CREATED_AT.** Threads are
  mutable: a two-year-old issue picks up a comment today. CREATED_AT ordering would
  never show it again, so the scheduled pass walks UPDATED_AT and stops at the
  high-water mark. Backfill keeps CREATED_AT because it is stable under a cursor --
  rows cannot reorder mid-crawl and make a resumed page skip items.
* **Threads are capped, and the cap is recorded.** The page query takes the first
  `comments` of each thread and flags anything longer as `comments_truncated`;
  `fill_capped_threads` then pages exactly those threads start to finish, so nothing
  is ever silently discarded -- megathreads just cost their own queries, on purpose,
  instead of inflating every page. Replies, reviews and inline code comments are
  completed inline during the harvest: they are small, and a cap there had no flag.
* **Reviews and replies are comments too.** A PR reviewer who never left an ordinary
  comment is still involved, and the person whose discussion reply was accepted as the
  answer is the most involved person in the thread. Both land in `thread_comments`
  with a `comment_type`, so "who is involved here" stays one query.

Comment authors arrive as login + type only, deliberately: the `UserFields` fragment
would multiply every profile field by the thread length, and anyone worth contacting is
already profiled by the interaction crawl. A commenter who has never otherwise touched
your repos therefore lands in `thread_comments` without a `users` row -- they show up
in the thread export with an empty name, which is honest, rather than being dropped.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .graphql import GraphQLClient, MissingScopeError, Page
from .ingest import parse_repo
from .ingest_gql import (
    SKIP_LOGINS,
    EmailScope,
    _Item,
    _record_profiles,
    looks_like_bot,
    profile_from_node,
)
from .models import THREAD_KINDS, Interaction, Profile, ThreadComment, ThreadRecord
from .queries import (
    DISCUSSION_REPLIES_FILL,
    PR_REVIEWS_FILL,
    PR_REVIEW_THREADS,
    REPO_INFO,
    THREAD_ORDER_CREATED,
    THREAD_ORDER_UPDATED,
    alias_for,
    fill_query,
    profiles_query,
    social_query,
    thread_query,
)
from .storage import Store

log = logging.getLogger(__name__)

#: Threads per page. Far below the usual 100: each node drags a body, ten labels,
#: twenty participants and a comment slice behind it, so 100 would be a heavy query
#: for no extra throughput. `gitscout doctor` prints what a page really costs.
DEFAULT_PAGE_SIZE = 25

#: Comments stored per thread before it is marked truncated.
DEFAULT_COMMENTS = 20

#: On an incremental run with no high-water mark yet, stop once this many threads in a
#: row come back byte-identical to what is already stored. Without it the first
#: incremental run after a backfill would walk the entire repo again.
UNCHANGED_STREAK = 50


@dataclass
class ThreadHarvestResult:
    repo: str
    kind: str
    fetched: int = 0
    new: int = 0
    comments_new: int = 0
    points: int = 0
    pages: int = 0
    complete: bool = False
    stopped_early: bool = False
    skipped: bool = False
    truncated: int = 0
    total_count: int | None = None

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped (done)"
        if self.stopped_early:
            return "up to date"
        return "complete" if self.complete else "partial"


# ------------------------------------------------------------------- mapping


def _logins(connection: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Logins from a `{nodes: [{login}]}` connection, bots and blanks dropped."""
    if not isinstance(connection, Mapping):
        return ()
    out: list[str] = []
    for node in connection.get("nodes") or []:
        login = (node or {}).get("login")
        if login and login not in SKIP_LOGINS and login not in out:
            out.append(login)
    return tuple(out)


def _names(connection: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(connection, Mapping):
        return ()
    return tuple(
        (node or {}).get("name")
        for node in (connection.get("nodes") or [])
        if (node or {}).get("name")
    )


def _author(node: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(login, type) of an author. GraphQL gives `author: null` for deleted accounts."""
    author = node.get("author") or {}
    login = author.get("login")
    if not login or login in SKIP_LOGINS:
        return None, None
    return login, ("Bot" if looks_like_bot(login) else author.get("__typename") or "User")


def _total(connection: Mapping[str, Any] | None) -> int | None:
    return connection.get("totalCount") if isinstance(connection, Mapping) else None


def _comment(
    repo: str,
    kind: str,
    number: int,
    raw: Mapping[str, Any],
    *,
    comment_type: str,
    position: int,
    parent_id: str | None = None,
) -> ThreadComment:
    login, login_type = _author(raw)
    return ThreadComment(
        repo=repo,
        kind=kind,
        number=number,
        comment_id=raw["id"],
        comment_type=comment_type,
        author=login,
        author_type=login_type,
        author_association=raw.get("authorAssociation"),
        body=raw.get("body") or None,
        created_at=raw.get("createdAt") or raw.get("submittedAt"),
        updated_at=raw.get("updatedAt"),
        position=position,
        is_answer=raw.get("isAnswer"),
        upvotes=raw.get("upvoteCount"),
        review_state=raw.get("state") if comment_type == "review" else None,
        parent_id=parent_id,
        url=raw.get("url"),
        file_path=raw.get("path"),
    )


def thread_from_node(
    repo: str, kind: str, node: Mapping[str, Any]
) -> tuple[ThreadRecord, list[ThreadComment]]:
    """One node of a thread-details connection -> the row plus its stored comments.

    ``position`` counts within each ``comment_type`` separately: comments 0..n-1,
    reviews 0..n-1, and a reply 0..n-1 within its parent. Interleaving them into one
    sequence would make the tail pass -- which fetches `last: N` of the *comments*
    connection and stores them at ``total - N`` -- collide with review positions.
    """
    number = int(node["number"])
    login, login_type = _author(node)
    thread = node.get("thread") or {}

    comments: list[ThreadComment] = []
    for position, raw in enumerate(thread.get("nodes") or []):
        if not raw or not raw.get("id"):
            continue
        comments.append(
            _comment(repo, kind, number, raw, comment_type="comment", position=position)
        )
        for reply_index, reply in enumerate(((raw.get("replies") or {}).get("nodes") or [])):
            if reply and reply.get("id"):
                comments.append(
                    _comment(
                        repo, kind, number, reply,
                        comment_type="reply", position=reply_index, parent_id=raw["id"],
                    )
                )

    for position, raw in enumerate((node.get("reviews") or {}).get("nodes") or []):
        # A review with no body is still a verdict, so it is kept: `review_state`
        # carries APPROVED / CHANGES_REQUESTED even when nothing was written.
        if raw and raw.get("id"):
            comments.append(
                _comment(repo, kind, number, raw, comment_type="review", position=position)
            )

    # Inline code-review comments. The review they belong to becomes `parent_id`, the
    # file they sit on `file_path` -- both straight from the node, nothing inferred.
    position = 0
    for thread_node in ((node.get("reviewThreads") or {}).get("nodes") or []):
        for raw in ((thread_node or {}).get("comments") or {}).get("nodes") or []:
            if raw and raw.get("id"):
                comments.append(
                    _comment(
                        repo, kind, number, raw,
                        comment_type="review_comment",
                        position=position,
                        parent_id=((raw.get("pullRequestReview") or {}).get("id")),
                    )
                )
                position += 1

    stored_comments = sum(1 for c in comments if c.comment_type == "comment")
    total_comments = _total(thread)
    answer = node.get("answer") or {}
    answer_login, _ = _author(answer) if answer else (None, None)

    record = ThreadRecord(
        repo=repo,
        kind=kind,
        number=number,
        url=node.get("url"),
        title=node.get("title"),
        body=node.get("body") or None,
        state=node.get("state"),
        state_reason=node.get("stateReason"),
        author=login,
        author_type=login_type,
        author_association=node.get("authorAssociation"),
        created_at=node.get("createdAt"),
        updated_at=node.get("updatedAt") or node.get("lastEditedAt"),
        closed_at=node.get("closedAt"),
        locked=node.get("locked"),
        comment_count=total_comments,
        reaction_count=_total(node.get("reactions")),
        labels=_names(node.get("labels")),
        assignees=_logins(node.get("assignees")),
        milestone=(node.get("milestone") or {}).get("title"),
        participants=_logins(node.get("participants")),
        node_id=node.get("id"),
        comments_stored=stored_comments,
        comments_truncated=bool(
            total_comments is not None and total_comments > stored_comments
        ),
        # pull requests
        merged=node.get("merged"),
        merged_at=node.get("mergedAt"),
        merged_by=(node.get("mergedBy") or {}).get("login"),
        is_draft=node.get("isDraft"),
        additions=node.get("additions"),
        deletions=node.get("deletions"),
        changed_files=node.get("changedFiles"),
        commits=_total(node.get("commits")),
        base_ref=node.get("baseRefName"),
        head_ref=node.get("headRefName"),
        review_decision=node.get("reviewDecision"),
        # discussions
        category=(node.get("category") or {}).get("name"),
        category_emoji=(node.get("category") or {}).get("emoji"),
        is_answerable=(node.get("category") or {}).get("isAnswerable"),
        # Discussions report `closed` directly; issues and PRs carry it in `state`.
        # Filling it for all three means "is this finished?" is one column, not two.
        closed=(
            node.get("closed")
            if node.get("closed") is not None
            else (node["state"] in ("CLOSED", "MERGED") if node.get("state") else None)
        ),
        upvotes=node.get("upvoteCount"),
        # `isAnswered` is null in categories that cannot take an answer, so fall back
        # to whether an answer node is actually attached.
        is_answered=(
            node.get("isAnswered")
            if node.get("isAnswered") is not None
            else (bool(answer) if "answer" in node else None)
        ),
        answer_author=answer_login,
        answer_at=answer.get("createdAt") if answer else None,
    )
    return record, comments


def _extract(repo: str, kind: str, page: Page) -> list[tuple[ThreadRecord, list[ThreadComment]]]:
    return [
        thread_from_node(repo, kind, node)
        for node in page.nodes
        if node.get("number") is not None
    ]


# ---------------------------------------------------------------- harvesting


async def harvest_threads(
    client: GraphQLClient,
    store: Store,
    repo: str,
    kind: str,
    *,
    max_items: int = 0,
    comments: int = DEFAULT_COMMENTS,
    page_size: int = DEFAULT_PAGE_SIZE,
    incremental: bool = False,
    fresh: bool = False,
    unchanged_streak: int = UNCHANGED_STREAK,
) -> ThreadHarvestResult:
    """Collect full records for one (repo, kind).

    Backfill resumes from a saved cursor and walks to the end; ``incremental=True``
    starts at page 1 in UPDATED_AT order and stops at the stored high-water mark.
    Every write is idempotent, so an interrupted run is simply re-run.
    """
    if kind not in THREAD_KINDS:
        raise ValueError(f"kind must be one of {', '.join(THREAD_KINDS)} (got {kind!r})")

    repo = parse_repo(repo)
    owner, name = repo.split("/", 1)
    result = ThreadHarvestResult(repo, kind)

    cursor_key = f"threads:{repo}:{kind}"
    water_key = f"threads:{repo}:{kind}:updated"
    if fresh:
        store.clear_crawl_state(cursor_key)
        store.clear_crawl_state(water_key)

    cursor_state = store.get_crawl_state(cursor_key) or {}
    if cursor_state.get("done") and not incremental:
        log.info("%s already fully harvested (use --fresh to redo, or --incremental)", cursor_key)
        result.skipped = result.complete = True
        return result

    high_water: str | None = (store.get_crawl_state(water_key) or {}).get("high_water")
    after = None if incremental else cursor_state.get("cursor")
    order = THREAD_ORDER_UPDATED if incremental else THREAD_ORDER_CREATED
    max_pages = math.ceil(max_items / page_size) if max_items else 0

    known_updated: dict[int, str | None] = {}
    if incremental and not high_water:
        known_updated = store.thread_updated_map(repo, kind)
    streak = 0
    newest_updated: str | None = None

    query, path = thread_query(kind)
    pages = client.paginate(
        query,
        {"owner": owner, "name": name, "comments": max(1, comments), "order": order},
        path,
        page_size=page_size,
        after=after,
        max_pages=max_pages,
    )

    async for page in pages:
        result.points += page.cost
        result.pages += 1
        if result.total_count is None:
            result.total_count = page.connection.get("totalCount")

        batch = _extract(repo, kind, page)
        if incremental:
            kept: list[tuple[ThreadRecord, list[ThreadComment]]] = []
            for record, thread in batch:
                updated = record.updated_at or ""
                if high_water and updated and updated <= high_water:
                    result.stopped_early = True
                    break
                if known_updated and known_updated.get(record.number) == record.updated_at:
                    streak += 1
                    if streak >= unchanged_streak:
                        result.stopped_early = True
                        break
                    continue
                streak = 0
                kept.append((record, thread))
            batch = kept
            if result.pages == 1 and page.nodes:
                newest_updated = (
                    max(
                        (n.get("updatedAt") or n.get("lastEditedAt") or "" for n in page.nodes),
                        default=None,
                    )
                    or None
                )

        records = [record for record, _ in batch]
        result.fetched += len(records)
        result.new += store.upsert_threads(records)
        result.truncated += sum(1 for r in records if r.comments_truncated)
        result.comments_new += store.add_thread_comments(
            [c for _, thread in batch for c in thread]
        )
        # Inner connections (replies, reviews, review threads) are completed here and
        # now; only the main comment thread is allowed to stay capped, with its flag.
        kept_numbers = {record.number for record, _ in batch}
        for node in page.nodes:
            if node.get("number") in kept_numbers:
                extra_points, extra_comments = await _complete_node_overflow(
                    client, store, repo, kind, node
                )
                result.points += extra_points
                result.comments_new += extra_comments

        if not incremental:
            store.set_crawl_state(
                cursor_key,
                cursor=page.cursor,
                done=not page.has_next,
                total_count=result.total_count,
            )
        if not page.has_next:
            result.complete = True

        log.info(
            "%s: page %d +%d %s, +%d comments (%d points)",
            cursor_key, result.pages, result.new, kind, result.comments_new, result.points,
        )
        if result.stopped_early:
            await pages.aclose()
            break

    if newest_updated:
        store.set_crawl_state(water_key, high_water=newest_updated)

    if not result.pages:
        log.warning("%s: nothing returned (repo not found, or this section is empty)", cursor_key)
    return result


@dataclass
class ThreadTotals:
    results: list[ThreadHarvestResult] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(r.new for r in self.results)

    @property
    def comments_new(self) -> int:
        return sum(r.comments_new for r in self.results)

    @property
    def points(self) -> int:
        return sum(r.points for r in self.results)


async def harvest_repos(
    client: GraphQLClient,
    store: Store,
    repos: Iterable[str],
    kinds: Sequence[str] = THREAD_KINDS,
    *,
    max_items: int = 0,
    comments: int = DEFAULT_COMMENTS,
    page_size: int = DEFAULT_PAGE_SIZE,
    incremental: bool = False,
    fresh: bool = False,
) -> ThreadTotals:
    """Harvest each (repo, kind) in turn. Sequential: points are one shared budget."""
    totals = ThreadTotals()
    for raw in repos:
        for kind in kinds:
            totals.results.append(
                await harvest_threads(
                    client, store, raw, kind,
                    max_items=max_items,
                    comments=comments,
                    page_size=page_size,
                    incremental=incremental,
                    fresh=fresh,
                )
            )
    return totals


#: Profiles looked up per GraphQL query, matching the events path.
PROFILE_BATCH = 25


async def resolve_participants(
    client: GraphQLClient,
    store: Store,
    *,
    limit: int = 0,
    scope: EmailScope | None = None,
) -> tuple[int, int, int]:
    """Fetch profiles for people who only ever appear inside a thread.

    The interaction crawl discovers whoever starred, forked, filed or committed. It
    never sees someone whose only trace is a comment on someone else's issue -- and
    without a `users` row there is no profile, no public email, and the enrichment
    stage will not even consider them.

    This closes that gap: every thread author, commenter, reviewer and replier with
    no profile is looked up, 25 per query. Returns (looked up, profiles stored,
    new email rows).
    """
    scope = scope if scope is not None else EmailScope()
    logins = store.thread_participants_without_profile(limit=limit)
    if not logins:
        return 0, 0, 0

    written = 0
    emails_new = 0
    for start in range(0, len(logins), PROFILE_BATCH):
        batch = logins[start : start + PROFILE_BATCH]
        query, variables = profiles_query(batch, scope.enabled)
        try:
            result = await client.execute(query, variables)
        except MissingScopeError:
            if not scope.enabled:
                raise
            scope.downgrade()
            query, variables = profiles_query(batch, include_email=False)
            result = await client.execute(query, variables)

        items: list[_Item] = []
        for index, login in enumerate(batch):
            node = result.data.get(alias_for(index))
            if not node:
                # A deleted or renamed account: record the absence so the next run
                # does not look it up again.
                store.upsert_profile(Profile(login=login, found=False))
                continue
            profile = profile_from_node(node)
            if profile is not None:
                items.append(_Item(Interaction("", profile.login, "threads"), profile))
        page_written, page_emails = _record_profiles(store, items)
        written += page_written
        emails_new += page_emails
        log.info(
            "participants: %d/%d looked up, +%d profiles, +%d emails",
            min(start + PROFILE_BATCH, len(logins)), len(logins), written, emails_new,
        )
    return len(logins), written, emails_new


# ------------------------------------------------------------ profile extras

_LINKEDIN = re.compile(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", re.IGNORECASE)


def normalize_linkedin(url: str | None) -> str | None:
    """`http://LinkedIn.com/in/Jane-Doe/?trk=x` -> `https://www.linkedin.com/in/jane-doe`.

    One canonical spelling, so the same profile matches the CRM's `LinkedinUrl` and
    the do-not-contact list regardless of how the person typed it. Company pages and
    anything that is not a personal profile are rejected rather than guessed at.
    """
    if not url:
        return None
    match = _LINKEDIN.search(url)
    if not match:
        return None
    slug = match.group(1).strip().strip("/").lower()
    return f"https://www.linkedin.com/in/{slug}" if slug else None


async def fetch_repo_info(client: GraphQLClient, store: Store, repo: str) -> dict[str, Any] | None:
    """Fetch and store one repository's stable id, owner, url and headline numbers."""
    repo = parse_repo(repo)
    owner, name = repo.split("/", 1)
    result = await client.execute(REPO_INFO, {"owner": owner, "name": name})
    node = result.pluck("repository")
    if not isinstance(node, Mapping):
        return None
    info = {
        "repo": node.get("nameWithOwner") or repo,
        "repository_id": node.get("databaseId"),
        "node_id": node.get("id"),
        "owner": (node.get("owner") or {}).get("login"),
        "name": node.get("name"),
        "url": node.get("url"),
        "description": node.get("description"),
        "homepage": node.get("homepageUrl") or None,
        "stars": node.get("stargazerCount"),
        "forks": node.get("forkCount"),
        "language": (node.get("primaryLanguage") or {}).get("name"),
        "topics": json.dumps(
            [t["topic"]["name"] for t in (node.get("repositoryTopics") or {}).get("nodes") or []]
        ),
        "is_archived": int(bool(node.get("isArchived"))),
    }
    store.upsert_repository(info)
    return info


async def resolve_social(
    client: GraphQLClient, store: Store, *, limit: int = 0, refresh: bool = False
) -> tuple[int, int]:
    """Look up the LinkedIn link and public repo count people publish on GitHub.

    Only what the person put on their own profile: `socialAccounts`, falling back to a
    LinkedIn URL typed into the website field. Nothing is inferred from a name.
    Returns (people looked up, LinkedIn links found).
    """
    logins = store.logins_needing_social(limit=limit, refresh=refresh)
    found = 0
    for start in range(0, len(logins), PROFILE_BATCH):
        batch = logins[start : start + PROFILE_BATCH]
        query, variables = social_query(batch)
        result = await client.execute(query, variables)
        blogs = {
            r["login"]: r["blog"]
            for r in store._conn.execute(
                f"SELECT login, blog FROM users WHERE login IN ({','.join('?' * len(batch))})",
                batch,
            )
        }
        for index, login in enumerate(batch):
            node = result.data.get(alias_for(index)) or {}
            linkedin = None
            for account in (node.get("socialAccounts") or {}).get("nodes") or []:
                if (account or {}).get("provider") == "LINKEDIN":
                    linkedin = normalize_linkedin(account.get("url"))
                    if linkedin:
                        break
            linkedin = linkedin or normalize_linkedin(blogs.get(login))
            repos = (node.get("repositories") or {}).get("totalCount")
            store.set_social(login, linkedin_url=linkedin, public_repos=repos)
            found += 1 if linkedin else 0
        log.info(
            "social: %d/%d looked up, %d LinkedIn links",
            min(start + PROFILE_BATCH, len(logins)), len(logins), found,
        )
    return len(logins), found


# -------------------------------------------------- completing capped content

async def _drain(
    client: GraphQLClient,
    query: str,
    variables: dict[str, Any],
    path: tuple[str, ...],
    *,
    after: str | None,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Walk one connection from `after` to its end. Returns (nodes, points)."""
    nodes: list[dict[str, Any]] = []
    points = 0
    cursor = after
    while True:
        result = await client.execute(
            query, {**variables, "first": page_size, "after": cursor}
        )
        points += result.cost
        connection = result.pluck(*path)
        if not isinstance(connection, Mapping):
            break
        nodes.extend(n for n in (connection.get("nodes") or []) if n)
        info = connection.get("pageInfo") or {}
        cursor = info.get("endCursor")
        if not info.get("hasNextPage") or not cursor:
            break
    return nodes, points


async def _complete_node_overflow(
    client: GraphQLClient, store: Store, repo: str, kind: str, node: Mapping[str, Any]
) -> tuple[int, int]:
    """Fetch what a page node's inner connections could not carry.

    Replies, reviews and review-thread comments are completed here, inline during the
    harvest, because they are small and the alternative is bookkeeping a truncation
    flag per inner connection. The main comment thread is NOT completed here -- that
    stays behind `comments_truncated` + `fill_capped_threads`, which is the deliberate
    cost control for 700-comment megathreads.
    """
    owner, name = repo.split("/", 1)
    number = int(node["number"])
    points = 0
    rows: list[ThreadComment] = []

    # discussion replies past the per-comment page
    for raw in ((node.get("thread") or {}).get("nodes") or []):
        replies = (raw or {}).get("replies") or {}
        info = replies.get("pageInfo") or {}
        if info.get("hasNextPage") and raw.get("id"):
            extra, cost = await _drain(
                client, DISCUSSION_REPLIES_FILL, {"id": raw["id"]},
                ("node", "replies"), after=info.get("endCursor"),
            )
            points += cost
            offset = len(replies.get("nodes") or [])
            rows.extend(
                _comment(repo, kind, number, reply, comment_type="reply",
                         position=offset + index, parent_id=raw["id"])
                for index, reply in enumerate(extra) if reply.get("id")
            )

    # PR reviews past the page
    reviews = node.get("reviews") or {}
    info = reviews.get("pageInfo") or {}
    if info.get("hasNextPage"):
        extra, cost = await _drain(
            client, PR_REVIEWS_FILL, {"owner": owner, "name": name, "number": number},
            ("repository", "pullRequest", "reviews"), after=info.get("endCursor"),
        )
        points += cost
        offset = len(reviews.get("nodes") or [])
        rows.extend(
            _comment(repo, kind, number, raw, comment_type="review", position=offset + index)
            for index, raw in enumerate(extra) if raw.get("id")
        )

    # review threads past the page (or inner comment pages)
    threads_conn = node.get("reviewThreads") or {}
    inner_overflow = any(
        (((t or {}).get("comments") or {}).get("pageInfo") or {}).get("hasNextPage")
        for t in (threads_conn.get("nodes") or [])
    )
    if (threads_conn.get("pageInfo") or {}).get("hasNextPage") or inner_overflow:
        threads, cost = await _drain(
            client, PR_REVIEW_THREADS, {"owner": owner, "name": name, "number": number},
            ("repository", "pullRequest", "reviewThreads"), after=None, page_size=25,
        )
        points += cost
        position = 0
        for thread_node in threads:
            for raw in (((thread_node or {}).get("comments") or {}).get("nodes") or []):
                if raw and raw.get("id"):
                    rows.append(
                        _comment(repo, kind, number, raw, comment_type="review_comment",
                                 position=position,
                                 parent_id=(raw.get("pullRequestReview") or {}).get("id"))
                    )
                    position += 1

    added = store.add_thread_comments(rows) if rows else 0
    return points, added


async def fill_capped_threads(
    client: GraphQLClient,
    store: Store,
    repo: str | None = None,
    kind: str | None = None,
    *,
    limit: int = 0,
    page_size: int = 100,
) -> tuple[int, int, int]:
    """Complete every thread flagged `comments_truncated`, start to finish.

    Replaces the old newest-slice "tail" approach, which deliberately left the middle
    of long threads unfetched. This walks the whole comments connection; the writes
    are idempotent, so the overlap with what is already stored costs nothing. Returns
    (threads completed, comments added, points).
    """
    pending = store.threads_needing_tail(repo, kind, limit=limit)
    done = added = points = 0
    for row in pending:
        row_kind = row["kind"]
        owner, name = row["repo"].split("/", 1)
        query, path = fill_query(row_kind)
        nodes, cost = await _drain(
            client, query, {"owner": owner, "name": name, "number": int(row["number"])},
            path, after=None, page_size=page_size,
        )
        points += cost
        rows: list[ThreadComment] = []
        for position, raw in enumerate(nodes):
            if not raw.get("id"):
                continue
            rows.append(
                _comment(row["repo"], row_kind, int(row["number"]), raw,
                         comment_type="comment", position=position)
            )
            replies = raw.get("replies") or {}
            reply_nodes = [r for r in (replies.get("nodes") or []) if r and r.get("id")]
            if (replies.get("pageInfo") or {}).get("hasNextPage"):
                extra, cost = await _drain(
                    client, DISCUSSION_REPLIES_FILL, {"id": raw["id"]},
                    ("node", "replies"), after=(replies.get("pageInfo") or {}).get("endCursor"),
                )
                points += cost
                reply_nodes.extend(r for r in extra if r.get("id"))
            rows.extend(
                _comment(row["repo"], row_kind, int(row["number"]), reply,
                         comment_type="reply", position=reply_index, parent_id=raw["id"])
                for reply_index, reply in enumerate(reply_nodes)
            )
        added += store.add_thread_comments(rows)
        stored = store.comments_stored(row["repo"], row_kind, int(row["number"]))
        store.mark_comments_stored(
            row["repo"], row_kind, int(row["number"]),
            stored=stored, truncated=stored < len(nodes),
        )
        done += 1
        log.info("fill %s/%s/#%s: %d comments, total points %d",
                 row["repo"], row_kind, row["number"], len(nodes), points)
    return done, added, points
