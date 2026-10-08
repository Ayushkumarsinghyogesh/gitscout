"""Plain data containers shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field

#: Interaction kinds the REST path supports.
KINDS: tuple[str, ...] = ("stars", "forks", "issues")
#: Everything the GraphQL path supports, weakest signal to strongest.
#: `prs` and `contribs` are the two "contributing" signals: `prs` is anyone who opened
#: one, `contribs` is whoever actually landed commits on the default branch.
ALL_KINDS: tuple[str, ...] = KINDS + ("prs", "discussions", "contribs")

#: Which API backend to use for a run.
BACKENDS: tuple[str, ...] = ("graphql", "rest", "apify")

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
