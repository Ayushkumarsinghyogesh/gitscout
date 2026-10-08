"""Stage 1 over GraphQL: stars / forks / issues / PRs, with profiles for free.

Two things make this path different from the REST one in [ingest.py]:

* **The profile comes with the interaction.** The ``UserFields`` fragment rides along
  in the same query, so 100 stargazers arrive fully profiled -- including their public
  email -- for ~1 point. The REST path needs 1 + 100 requests for the same data.
* **Newest first, with an early stop.** Every connection is ordered DESC, so a
  scheduled run reads page 1, halts at the first interaction it has already recorded
  (the stored high-water mark), and costs ~2 points. That is what makes a 15-minute
  cron affordable.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .emails import normalize_email, score
from .graphql import GraphQLClient, MissingScopeError, Page
from .ingest import parse_repo
from .models import ALL_KINDS, EmailCandidate, Interaction, Profile
from .queries import KIND_BODIES, kind_query
from .storage import Store

log = logging.getLogger(__name__)

SKIP_LOGINS = {"ghost"}


class EmailScope:
    """Tracks whether the token may read ``User.email``, for the life of a run.

    A token without ``read:user`` fails the whole query -- and only once it meets a
    user who actually has an address set, so it dies partway through a crawl. Rather
    than abort the job, the crawler drops the field and carries on; the warning is
    logged once so it is obvious why profile emails stopped appearing.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.downgraded = False

    def downgrade(self) -> None:
        if self.enabled:
            self.enabled = False
            self.downgraded = True
            log.warning(
                "This token cannot read profile emails: GraphQL's User.email needs the "
                "'read:user' scope. Continuing without it -- stars/forks/issues/PRs and "
                "commit emails all still work, but profile emails will be missed. "
                "Add the scope at https://github.com/settings/tokens and re-run."
            )


@dataclass
class GqlIngestResult:
    repo: str
    kind: str
    fetched: int = 0
    new: int = 0
    profiles: int = 0
    emails_new: int = 0
    points: int = 0
    pages: int = 0
    complete: bool = False
    stopped_early: bool = False
    skipped: bool = False
    total_count: int | None = None

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped (done)"
        if self.stopped_early:
            return "up to date"
        return "complete" if self.complete else "partial"


def _user_type(node_type: str | None, login: str) -> str | None:
    """Account type for the interaction row.

    Automation on a machine-user account reports ``__typename: "User"``, so it is
    relabelled ``Bot`` here. The interaction is still stored -- only the lead export
    filters on this.
    """
    return "Bot" if looks_like_bot(login) else node_type


@dataclass
class _Item:
    """One interaction, the profile that came with it, and any email it carried."""

    interaction: Interaction
    profile: Profile | None = None
    emails: list[EmailCandidate] = field(default_factory=list)


#: GitHub App bots use the ``name[bot]`` convention, but plenty of automation runs on
#: ordinary *machine user* accounts (`prowler-bot`, `renovate-bot`) whose ``__typename``
#: is ``User``. Those dominate the commit log of an active repo, so the type check alone
#: is not enough.
BOT_LOGIN_SUFFIXES = ("[bot]", "-bot", "_bot")

#: Well-known automation accounts whose login carries no delimiter.
KNOWN_BOT_LOGINS = frozenset(
    {
        "dependabot",
        "renovate",
        "greenkeeper",
        "codecov",
        "mergify",
        "imgbot",
        "allcontributors",
        "semantic-release",
        "pre-commit-ci",
        "github-actions",
        "snyk-bot",
        "netlify",
        "vercel",
        "sonarcloud",
        "stale",
        "cla-assistant",
        "web-flow",  # GitHub's own web-UI commit author
    }
)


def looks_like_bot(login: str | None) -> bool:
    """Is this login automation rather than a person?

    Interactions from these accounts are still recorded -- a bot opening PRs is a fact
    about the repo -- they are just never exported as leads. If this ever catches a real
    person, they are still in the database: ``gitscout db --table users`` will show them.
    """
    if not login:
        return False
    name = login.lower()
    return name in KNOWN_BOT_LOGINS or name.endswith(BOT_LOGIN_SUFFIXES)


def profile_from_node(node: Mapping[str, Any]) -> Profile | None:
    """Build a Profile from a ``UserFields`` fragment. None for bots/orgs/unknowns."""
    login = node.get("login")
    if not login or looks_like_bot(login):
        return None
    typename = node.get("__typename") or "User"
    if typename != "User":
        return None
    followers = node.get("followers") or {}
    # GraphQL returns "" rather than null for an unset email/name/company.
    blank_to_none = lambda v: (v or None) if isinstance(v, str) else v  # noqa: E731
    return Profile(
        login=login,
        type="User",
        name=blank_to_none(node.get("name")),
        company=blank_to_none(node.get("company")),
        bio=blank_to_none(node.get("bio")),
        location=blank_to_none(node.get("location")),
        blog=blank_to_none(node.get("websiteUrl")),
        twitter=blank_to_none(node.get("twitterUsername")),
        public_email=blank_to_none(node.get("email")),
        hireable=node.get("isHireable"),
        followers=followers.get("totalCount") if isinstance(followers, Mapping) else None,
        node_id=node.get("id"),
        created_at=node.get("createdAt"),
    )


# ---- per-kind extraction: a connection page -> ordered _Item list -------------------


def _extract_stars(repo: str, page: Page) -> list[_Item]:
    out: list[_Item] = []
    for edge in page.edges:
        node = edge.get("node") or {}
        login = node.get("login")
        if not login or login in SKIP_LOGINS:
            continue
        out.append(
            _Item(
                Interaction(
                    repo, login, "stars", _user_type(node.get("__typename"), login),
                    edge.get("starredAt"),
                ),
                profile_from_node(node),
            )
        )
    return out


def _extract_forks(repo: str, page: Page) -> list[_Item]:
    out: list[_Item] = []
    for node in page.nodes:
        owner = node.get("owner") or {}
        login = owner.get("login")
        if not login or login in SKIP_LOGINS:
            continue
        out.append(
            _Item(
                Interaction(
                    repo,
                    login,
                    "forks",
                    _user_type(owner.get("__typename"), login),
                    node.get("createdAt"),
                    node.get("nameWithOwner"),
                ),
                profile_from_node(owner),
            )
        )
    return out


def _extract_authored(kind: str):
    def extract(repo: str, page: Page) -> list[_Item]:
        out: list[_Item] = []
        for node in page.nodes:
            author = node.get("author") or {}
            login = author.get("login")
            if not login or login in SKIP_LOGINS:
                continue  # deleted account: GraphQL gives author: null
            out.append(
                _Item(
                    Interaction(
                        repo,
                        login,
                        kind,
                        _user_type(author.get("__typename"), login),
                        node.get("createdAt"),
                        node.get("url"),
                    ),
                    profile_from_node(author),
                )
            )
        return out

    return extract


def _extract_contribs(repo: str, page: Page) -> list[_Item]:
    """Commit authors on the default branch, with the email from the commit itself.

    GitHub has already linked ``author.user`` to the account, so attribution is exact
    and the address gets full confidence. Commits whose author is not linked to any
    account are skipped: there is no login to file the address under.
    """
    out: list[_Item] = []
    for node in page.nodes:
        author = node.get("author") or {}
        user = author.get("user") or {}
        login = user.get("login")
        if not login or login in SKIP_LOGINS:
            continue
        emails: list[EmailCandidate] = []
        email = normalize_email(author.get("email"))
        if email and not looks_like_bot(login):
            emails.append(EmailCandidate(email, "gql_contrib", score("commit_api")))
        out.append(
            _Item(
                Interaction(
                    repo,
                    login,
                    "contribs",
                    _user_type(user.get("__typename"), login),
                    node.get("authoredDate"),
                    node.get("oid"),
                ),
                profile_from_node(user),
                emails,
            )
        )
    return out


EXTRACTORS = {
    "stars": _extract_stars,
    "forks": _extract_forks,
    "issues": _extract_authored("issues"),
    "prs": _extract_authored("prs"),
    "discussions": _extract_authored("discussions"),
    "contribs": _extract_contribs,
}
assert set(EXTRACTORS) == set(ALL_KINDS)
assert set(EXTRACTORS) == set(KIND_BODIES)


def _record_profiles(store: Store, items: Sequence[_Item]) -> tuple[int, int]:
    """Persist the profiles that rode along, plus any public profile emails.

    Returns (profiles written, new email rows).
    """
    written = 0
    emails_new = 0
    for item in items:
        profile = item.profile
        if profile is None:
            continue
        store.upsert_profile(profile)
        written += 1

        candidates = list(item.emails)  # e.g. the commit address from `contribs`
        email = normalize_email(profile.public_email)
        if email:
            candidates.append(EmailCandidate(email, "gql_profile", score("profile")))
        if candidates:
            emails_new += store.add_emails(profile.login, candidates)
    return written, emails_new


async def ingest_kind_gql(
    client: GraphQLClient,
    store: Store,
    repo: str,
    kind: str,
    *,
    max_items: int = 0,
    fresh: bool = False,
    incremental: bool = False,
    page_size: int = 100,
    scope: EmailScope | None = None,
) -> GqlIngestResult:
    """Crawl one (repo, kind) over GraphQL.

    ``incremental=True`` is the cron mode: always start at page 1 and stop at the
    stored high-water mark. Backfill mode (the default) resumes from a saved cursor
    and walks to the end.

    If the token turns out to lack ``read:user``, the crawl restarts once from its last
    saved cursor with the ``email`` field dropped. Restarting is safe because every
    write is idempotent and the cursor is persisted per page.
    """
    if kind not in KIND_BODIES:
        raise ValueError(f"kind must be one of {', '.join(ALL_KINDS)} (got {kind!r})")

    scope = scope if scope is not None else EmailScope()
    repo = parse_repo(repo)

    if fresh:
        store.clear_crawl_state(f"gql:{repo}:{kind}")
    try:
        return await _crawl(
            client, store, repo, kind,
            max_items=max_items, incremental=incremental, page_size=page_size, scope=scope,
        )
    except MissingScopeError:
        if not scope.enabled:
            raise  # already without email: the scope is not the problem
        scope.downgrade()
        return await _crawl(
            client, store, repo, kind,
            max_items=max_items, incremental=incremental, page_size=page_size, scope=scope,
        )


async def _crawl(
    client: GraphQLClient,
    store: Store,
    repo: str,
    kind: str,
    *,
    max_items: int,
    incremental: bool,
    page_size: int,
    scope: EmailScope,
) -> GqlIngestResult:
    owner, name = repo.split("/", 1)
    query, path = kind_query(kind, scope.enabled)
    extract = EXTRACTORS[kind]
    key = f"gql:{repo}:{kind}"
    result = GqlIngestResult(repo, kind)

    state = store.get_crawl_state(key) or {}
    high_water: str | None = state.get("high_water")

    if state.get("done") and not incremental:
        log.info("%s already fully ingested (use --fresh to redo, or --incremental)", key)
        result.skipped = result.complete = True
        return result

    after = None if incremental else state.get("cursor")
    from_page_one = after is None
    max_pages = math.ceil(max_items / page_size) if max_items else 0
    newest_seen: str | None = None

    pages = client.paginate(
        query,
        {"owner": owner, "name": name},
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

        items = extract(repo, page)
        if from_page_one and result.pages == 1 and items:
            # DESC order: the first page holds the newest interaction overall.
            newest_seen = max((i.interaction.occurred_at or "" for i in items), default=None)

        if incremental and high_water:
            keep: list[_Item] = []
            for item in items:
                occurred = item.interaction.occurred_at or ""
                if occurred and occurred <= high_water:
                    result.stopped_early = True
                    break
                keep.append(item)
            items = keep

        result.fetched += len(items)
        result.new += store.add_interactions([i.interaction for i in items])
        written, emails_new = _record_profiles(store, items)
        result.profiles += written
        result.emails_new += emails_new

        if not incremental:
            store.set_crawl_state(
                key,
                cursor=page.cursor,
                done=not page.has_next,
                total_count=result.total_count,
            )
        if not page.has_next:
            result.complete = True

        log.info(
            "%s: page %d +%d new (%d fetched, %d points)",
            key,
            result.pages,
            result.new,
            result.fetched,
            result.points,
        )
        if result.stopped_early:
            await pages.aclose()
            break

    if newest_seen:
        store.set_crawl_state(key, high_water=newest_seen, total_count=result.total_count)
    elif result.pages and incremental:
        store.set_crawl_state(key, total_count=result.total_count)

    if not result.pages:
        log.warning("%s: no data (repo not found, empty, or not accessible)", key)
    return result


@dataclass
class IngestTotals:
    results: list[GqlIngestResult] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(r.new for r in self.results)

    @property
    def emails_new(self) -> int:
        return sum(r.emails_new for r in self.results)

    @property
    def points(self) -> int:
        return sum(r.points for r in self.results)


async def ingest_repos_gql(
    client: GraphQLClient,
    store: Store,
    repos: Iterable[str],
    kinds: Sequence[str],
    *,
    max_items: int = 0,
    fresh: bool = False,
    incremental: bool = False,
    page_size: int = 100,
    scope: EmailScope | None = None,
) -> IngestTotals:
    """Crawl every (repo, kind) in turn.

    Sequential on purpose: GraphQL points are a single shared budget, so running
    queries in parallel buys nothing and makes rate-limit backoff harder to reason
    about. The expensive per-user work is batched in [enrich_gql.py] instead.
    """
    totals = IngestTotals()
    scope = scope if scope is not None else EmailScope()
    for raw in repos:
        for kind in kinds:
            totals.results.append(
                await ingest_kind_gql(
                    client,
                    store,
                    raw,
                    kind,
                    max_items=max_items,
                    fresh=fresh,
                    incremental=incremental,
                    page_size=page_size,
                    scope=scope,
                )
            )
    return totals
