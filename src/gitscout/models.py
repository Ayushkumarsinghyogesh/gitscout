"""Plain data containers shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field

#: Interaction kinds the REST path supports.
KINDS: tuple[str, ...] = ("stars", "forks", "issues")
#: Everything the GraphQL path supports, weakest signal to strongest.
#: `prs` and `contribs` are the two "contributing" signals: `prs` is anyone who opened
#: one, `contribs` is whoever actually landed commits on the default branch.
ALL_KINDS: tuple[str, ...] = KINDS + ("prs", "discussions", "contribs")

#: GitHub restricted the stargazers *list* to repo admins/collaborators on
#: 2026-06-30 (the public star *count* still works). For any repo you do not
#: administer the connection comes back empty, so `stars` is excluded from the
#: default set -- asking for it just burns a query per repo per run. It stays in
#: ALL_KINDS because it does work on repos you own.
#: https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/
RESTRICTED_KINDS: frozenset[str] = frozenset({"stars"})
DEFAULT_KINDS: tuple[str, ...] = tuple(k for k in ALL_KINDS if k not in RESTRICTED_KINDS)

#: Account types that are never people.
NON_HUMAN_TYPES = frozenset({"Bot", "Organization", "Mannequin", "EnterpriseUserAccount"})


@dataclass(frozen=True)
class Interaction:
    """A GitHub user touching a repo: starred it, forked it, filed an issue or a PR."""

    repo: str
    login: str
    kind: str  # one of ALL_KINDS
    user_type: str | None = None  # "User", "Organization", "Bot"
    occurred_at: str | None = None
    extra: str | None = None  # fork full_name / issue or PR url


@dataclass
class Profile:
    login: str
    type: str | None = None
    name: str | None = None
    company: str | None = None
    bio: str | None = None
    location: str | None = None
    blog: str | None = None
    twitter: str | None = None
    public_email: str | None = None
    hireable: bool | None = None
    followers: int | None = None
    found: bool = True
    node_id: str | None = None  # GraphQL global id, needed for commit probes
    created_at: str | None = None  # account age is a useful lead signal


@dataclass(frozen=True)
class EmailCandidate:
    email: str
    source: str  # profile | commit_api | events | website | gql_profile | gql_commit | apify | hunter
    confidence: float


#: The repo sections the thread stage can harvest: the three that have a body and
#: a comment thread. `forks` and `contribs` are not threads -- they are recorded by
#: the interaction crawl alone.
THREAD_KINDS: tuple[str, ...] = ("issues", "prs", "discussions")

#: How a person is attached to a thread. All of them count as "involved"; the order
#: is the precedence used when one person did several things to the same thread, and
#: it is alphabetical on purpose so SQL ``MIN(role)`` resolves it without a CASE.
THREAD_ROLES: tuple[str, ...] = ("author", "commenter", "reviewer")


@dataclass
class ThreadRecord:
    """One issue, pull request or discussion, with everything GitHub will tell us.

    Stored separately from `Interaction` because `interactions` is keyed
    (repo, login, kind) -- one row per person per repo -- so it structurally cannot
    hold the dozen issues a single person filed.

    The kind-specific fields are plain nullable columns rather than a JSON blob: a
    CSV of pull requests should have an `additions` column you can sort on, not a
    string you have to parse.
    """

    repo: str
    kind: str  # issues | prs | discussions
    number: int
    url: str | None = None
    title: str | None = None
    body: str | None = None
    state: str | None = None  # OPEN | CLOSED | MERGED
    state_reason: str | None = None  # COMPLETED | NOT_PLANNED | REOPENED | DUPLICATE
    author: str | None = None  # None for a deleted account
    author_type: str | None = None
    author_association: str | None = None  # FIRST_TIME_CONTRIBUTOR, MEMBER, ...
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    locked: bool | None = None
    comment_count: int | None = None
    reaction_count: int | None = None
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    milestone: str | None = None
    participants: tuple[str, ...] = ()
    node_id: str | None = None
    comments_stored: int = 0
    comments_truncated: bool = False

    # --- pull requests only
    merged: bool | None = None
    merged_at: str | None = None
    merged_by: str | None = None
    is_draft: bool | None = None
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    commits: int | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    review_decision: str | None = None  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED

    # --- discussions only
    category: str | None = None
    category_emoji: str | None = None
    is_answerable: bool | None = None  # only Q&A-style categories accept an answer
    upvotes: int | None = None
    is_answered: bool | None = None
    answer_author: str | None = None
    answer_at: str | None = None

    #: Discussions report `closed` separately from `state`, which they do not have.
    closed: bool | None = None


@dataclass(frozen=True)
class ThreadComment:
    """One comment, review or reply.

    Reviews and replies live in the same table as ordinary comments so that "who is
    involved in this thread" stays a single query. `position` is the order within
    this comment's own type -- see `threads_gql.thread_from_node` for why.
    """

    repo: str
    kind: str
    number: int
    comment_id: str
    comment_type: str = "comment"  # comment | review | reply | review_comment
    author: str | None = None
    author_type: str | None = None
    author_association: str | None = None
    body: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    position: int = 0
    is_answer: bool | None = None  # discussions: this comment solved it
    upvotes: int | None = None
    review_state: str | None = None  # prs: APPROVED | CHANGES_REQUESTED | COMMENTED
    parent_id: str | None = None  # replies: parent comment; review_comments: the review
    url: str | None = None  # the comment's own GitHub link
    file_path: str | None = None  # review_comments: the file the thread is on


@dataclass(frozen=True)
class Target:
    """A repo to watch, from a target profile file or the CLI."""

    repo: str
    kinds: tuple[str, ...] = ALL_KINDS
    weight: float = 1.0
    note: str | None = None


@dataclass
class RunRecord:
    """One scheduled or manual run, for the audit log."""

    run_id: int | None = None
    mode: str = "scout"
    targets: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    interactions_new: int = 0
    users_new: int = 0
    emails_new: int = 0
    points_used: int = 0
    status: str = "running"
    error: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
