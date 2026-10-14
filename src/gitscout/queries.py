"""Every GraphQL document gitscout sends, in one place.

Kept together deliberately: GitHub's schema does change, and `gitscout doctor`
validates each of these against the live API, so a break is a one-file fix.

Two design notes worth knowing:

* Each interaction query orders **newest first** (``direction: DESC``). That is what
  makes the cron path cheap: a scheduled run reads page 1, stops at the first item it
  has already seen, and costs ~2 points instead of re-walking the whole list.
* The user profile is embedded in the *same* query as the interaction, via the
  ``UserFields`` fragment. The per-user ``GET /users/{login}`` call of the REST path
  disappears entirely -- 100 fully-profiled users for ~1 point.
"""
from __future__ import annotations

from typing import Any, Sequence

from .models import THREAD_KINDS

#: Fields pulled for every discovered account, except the email.
_USER_FIELD_NAMES = (
    "__typename",
    "login",
    "id",
    "databaseId",
    "name",
    "company",
    "location",
    "bio",
    "websiteUrl",
    "twitterUsername",
    "isHireable",
    "createdAt",
    "followers { totalCount }",
)

#: ``User.email`` is the cheapest, highest-confidence email source there is -- but it
#: needs the ``read:user`` (or ``user:email``) scope, and a token without it fails the
#: *entire* query rather than just omitting the field. Worse, GitHub only raises that
#: error when a user actually has an address set, so a scopeless token dies partway
#: through a crawl rather than at the start.
#:
#: So the fragment is built in two variants and the crawler falls back to the second
#: one if the scope turns out to be missing. Everything else still works; you just lose
#: profile emails (commit emails are unaffected -- they come from the git author line,
#: not the user object).
EMAIL_FIELD = "email"


def user_fields(include_email: bool = True) -> str:
    """The ``UserFields`` fragment, with or without the scope-gated email field."""
    names = list(_USER_FIELD_NAMES)
    if include_email:
        names.insert(5, EMAIL_FIELD)  # keep it next to `name`, for readability
    body = "\n".join(f"  {field}" for field in names)
    return "\nfragment UserFields on User {\n" + body + "\n}\n"


#: The default (with email), kept as a module constant for the common case. The
#: no-email variant is built on demand by `user_fields(False)`.
USER_FIELDS = user_fields(True)

_RATE_LIMIT = "  rateLimit { cost remaining resetAt limit nodeCount }"

_STARS_BODY = (
    """
query Stars($owner: String!, $name: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    stargazerCount
    stargazers(first: $first, after: $after, orderBy: {field: STARRED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      edges {
        starredAt
        node { ...UserFields }
      }
    }
  }
}
"""
)

_FORKS_BODY = (
    """
query Forks($owner: String!, $name: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    forks(first: $first, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        createdAt
        owner {
          __typename
          login
          ... on User { ...UserFields }
        }
      }
    }
  }
}
"""
)

_ISSUES_BODY = (
    """
query Issues($owner: String!, $name: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    issues(
      first: $first
      after: $after
      states: [OPEN, CLOSED]
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        url
        createdAt
        author {
          __typename
          login
          ... on User { ...UserFields }
        }
      }
    }
  }
}
"""
)

_PRS_BODY = (
    """
query PullRequests($owner: String!, $name: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(
      first: $first
      after: $after
      states: [OPEN, CLOSED, MERGED]
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        url
        createdAt
        merged
        author {
          __typename
          login
          ... on User { ...UserFields }
        }
      }
    }
  }
}
"""
)

_DISCUSSIONS_BODY = (
    """
query Discussions($owner: String!, $name: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    discussions(
      first: $first
      after: $after
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        url
        createdAt
        author {
          __typename
          login
          ... on User { ...UserFields }
        }
      }
    }
  }
}
"""
)

#: Commit authors on the target repo's default branch -- the people who actually
#: landed code. The cheapest email source in the whole tool: ``author.email`` is the
#: git author line, so a contributor arrives with login, profile *and* address in a
#: single query. The REST ``/contributors`` endpoint caps at 500 and carries no email.
#:
#: ``history`` takes no ``orderBy``: commit history is reverse-chronological by
#: definition, which is exactly the newest-first ordering the incremental crawl needs.
_CONTRIBUTORS_BODY = (
    """
query Contributors($owner: String!, $name: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    defaultBranchRef {
      name
      target {
        __typename
        ... on Commit {
          history(first: $first, after: $after) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              authoredDate
              author {
                name
                email
                user { ...UserFields }
              }
            }
          }
        }
      }
    }
  }
}
"""
)

#: Query body (fragment not yet attached) + the connection path to paginate.
KIND_BODIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "stars": (_STARS_BODY, ("repository", "stargazers")),
    "forks": (_FORKS_BODY, ("repository", "forks")),
    "issues": (_ISSUES_BODY, ("repository", "issues")),
    "prs": (_PRS_BODY, ("repository", "pullRequests")),
    "discussions": (_DISCUSSIONS_BODY, ("repository", "discussions")),
    "contribs": (_CONTRIBUTORS_BODY, ("repository", "defaultBranchRef", "target", "history")),
}


def kind_query(kind: str, include_email: bool = True) -> tuple[str, tuple[str, ...]]:
    """The document and connection path for one interaction kind.

    ``include_email=False`` drops the scope-gated ``User.email`` field so a token
    without ``read:user`` can still crawl everything else.
    """
    if kind not in KIND_BODIES:
        raise KeyError(kind)
    body, path = KIND_BODIES[kind]
    return body + user_fields(include_email), path


#: The default documents (with email), one per interaction kind.
KIND_QUERIES: dict[str, tuple[str, tuple[str, ...]]] = {
    kind: kind_query(kind, True) for kind in KIND_BODIES
}

#: Back-compatible single-query names.
STARS, FORKS, ISSUES, PRS, DISCUSSIONS, CONTRIBUTORS = (
    KIND_QUERIES[k][0] for k in ("stars", "forks", "issues", "prs", "discussions", "contribs")
)

#: Kinds whose connection is newest-first without an explicit `orderBy`.
NATURALLY_NEWEST_FIRST = frozenset({"contribs"})

# ------------------------------------------------------------- thread details

#: Full records for the three *discussion-shaped* sections of a repo -- issues, pull
#: requests and discussions -- with their comment threads.
#:
#: Deliberately separate documents from the interaction crawl in `KIND_BODIES`. That
#: crawl has to stay at ~1 point per 100 items or the cron path stops being
#: affordable; these ask for the body, labels, participants and a slice of the thread
#: as well. `gitscout doctor` reports what a page of each really costs.
#:
#: Comment authors arrive as login + __typename only. The UserFields fragment is
#: deliberately not used here: it would multiply every profile field by the thread
#: length, and anyone worth contacting is already profiled by the interaction crawl.
#:
#: Every connection carries an explicit `first`, including `reactions`: GitHub rejects
#: a connection without one, so `reactions(first: 1) { totalCount }` is the cheapest
#: legal way to read a count.

#: Shared by issues and pull requests -- both are `IssueComment` connections.
_ISSUE_COMMENT_FIELDS = """
          nodes {
            id
            createdAt
            updatedAt
            authorAssociation
            body
            author { __typename login }
          }
"""

#: `labels`, `assignees`, `participants` and `reactions` are identical on issues and
#: pull requests, so the selection is shared rather than copied.
_SHARED_THREAD_FIELDS = """
        reactions(first: 1) { totalCount }
        labels(first: 10) { totalCount nodes { name } }
        assignees(first: 5) { nodes { login } }
        milestone { title }
        participants(first: 20) { totalCount nodes { login } }
"""

_ISSUE_DETAILS_BODY = (
    """
query IssueDetails(
  $owner: String!
  $name: String!
  $first: Int!
  $after: String
  $comments: Int!
  $order: IssueOrder!
) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    issues(first: $first, after: $after, states: [OPEN, CLOSED], orderBy: $order) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        url
        title
        body
        state
        stateReason
        createdAt
        updatedAt
        closedAt
        locked
        authorAssociation
        author { __typename login }
"""
    + _SHARED_THREAD_FIELDS
    + """
        thread: comments(first: $comments) {
          totalCount
          pageInfo { hasNextPage endCursor }
"""
    + _ISSUE_COMMENT_FIELDS
    + """
        }
      }
    }
  }
}
"""
)

#: Pull requests add the review verdict and the size of the change -- both strong
#: signals about how involved someone actually is. Reviews are stored as thread
#: comments with ``comment_type = 'review'``, so a reviewer counts as involved even
#: when they never left an ordinary comment.
_PR_DETAILS_BODY = (
    """
query PullRequestDetails(
  $owner: String!
  $name: String!
  $first: Int!
  $after: String
  $comments: Int!
  $order: IssueOrder!
) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(
      first: $first
      after: $after
      states: [OPEN, CLOSED, MERGED]
      orderBy: $order
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        url
        title
        body
        state
        createdAt
        updatedAt
        closedAt
        mergedAt
        merged
        mergedBy { login }
        isDraft
        locked
        authorAssociation
        author { __typename login }
        additions
        deletions
        changedFiles
        baseRefName
        headRefName
        reviewDecision
        commits(first: 1) { totalCount }
"""
    + _SHARED_THREAD_FIELDS
    + """
        reviews(first: $comments) {
          totalCount
          nodes {
            id
            state
            body
            submittedAt
            updatedAt
            authorAssociation
            author { __typename login }
          }
        }
        thread: comments(first: $comments) {
          totalCount
          pageInfo { hasNextPage endCursor }
"""
    + _ISSUE_COMMENT_FIELDS
    + """
        }
      }
    }
  }
}
"""
)

#: Discussions are a different type: no assignees or milestone, but a category, an
#: upvote count and an accepted answer. ``isAnswer`` on a comment is the most useful
#: field in the section -- it marks the person who solved someone else's problem.
_DISCUSSION_DETAILS_BODY = (
    """
query DiscussionDetails(
  $owner: String!
  $name: String!
  $first: Int!
  $after: String
  $comments: Int!
  $order: DiscussionOrder!
) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    nameWithOwner
    discussions(first: $first, after: $after, orderBy: $order) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        url
        title
        body
        createdAt
        updatedAt
        lastEditedAt
        closed
        closedAt
        locked
        authorAssociation
        upvoteCount
        isAnswered
        author { __typename login }
        category { name emoji isAnswerable }
        answer { createdAt author { __typename login } }
        reactions(first: 1) { totalCount }
        labels(first: 10) { totalCount nodes { name } }
        thread: comments(first: $comments) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            createdAt
            updatedAt
            body
            isAnswer
            upvoteCount
            author { __typename login }
            replies(first: 5) {
              totalCount
              nodes {
                id
                createdAt
                updatedAt
                body
                author { __typename login }
              }
            }
          }
        }
      }
    }
  }
}
"""
)

#: kind -> (document, connection path, the `orderBy` input the connection accepts).
#: The kind names match the interaction kinds, so `-k issues,prs` means the same
#: thing in both stages.
THREAD_BODIES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "issues": (_ISSUE_DETAILS_BODY, ("repository", "issues"), "IssueOrder"),
    "prs": (_PR_DETAILS_BODY, ("repository", "pullRequests"), "IssueOrder"),
    "discussions": (
        _DISCUSSION_DETAILS_BODY,
        ("repository", "discussions"),
        "DiscussionOrder",
    ),
}

#: Backfill walks CREATED_AT: it never reorders under a cursor, so a resumed crawl
#: cannot skip or repeat pages. The incremental pass walks UPDATED_AT instead, which
#: is the only ordering that surfaces a two-year-old thread someone replied to today.
THREAD_ORDER_CREATED = {"field": "CREATED_AT", "direction": "DESC"}
THREAD_ORDER_UPDATED = {"field": "UPDATED_AT", "direction": "DESC"}


def thread_query(kind: str) -> tuple[str, tuple[str, ...]]:
    """The document and connection path for one thread kind."""
    if kind not in THREAD_BODIES:
        raise KeyError(kind)
    body, path, _order = THREAD_BODIES[kind]
    return body, path


#: The newest comments of a thread longer than the per-thread cap. Paging a
#: 400-comment thread forward would cost 20 queries for context nobody reads; the
#: tail is where the current state of the conversation lives, so we take `last` and
#: record that the middle is missing (`threads.comments_truncated`).
_TAIL_BODIES = {
    "issues": """
query IssueCommentsTail($owner: String!, $name: String!, $number: Int!, $last: Int!) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      comments(last: $last) {
        totalCount
"""
    + _ISSUE_COMMENT_FIELDS
    + """
      }
    }
  }
}
""",
    "prs": """
query PullRequestCommentsTail($owner: String!, $name: String!, $number: Int!, $last: Int!) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      comments(last: $last) {
        totalCount
"""
    + _ISSUE_COMMENT_FIELDS
    + """
      }
    }
  }
}
""",
    "discussions": """
query DiscussionCommentsTail($owner: String!, $name: String!, $number: Int!, $last: Int!) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number
      comments(last: $last) {
        totalCount
        nodes {
          id
          createdAt
          updatedAt
          body
          isAnswer
          upvoteCount
          author { __typename login }
        }
      }
    }
  }
}
""",
}

#: kind -> the path to the single thread a tail query returns.
TAIL_PATHS: dict[str, tuple[str, ...]] = {
    "issues": ("repository", "issue"),
    "prs": ("repository", "pullRequest"),
    "discussions": ("repository", "discussion"),
}


def tail_query(kind: str) -> tuple[str, tuple[str, ...]]:
    """The tail document and the path to the thread it returns."""
    if kind not in _TAIL_BODIES:
        raise KeyError(kind)
    return _TAIL_BODIES[kind], TAIL_PATHS[kind]


# --------------------------------------------------------------- commit emails

#: Probe a user's own (non-fork) repos for commit author emails.
#:
#: Deliberately *not* filtered with ``history(author: {id: ...})``: that filter only
#: matches commits GitHub has already linked to the account, which excludes exactly
#: the unlinked commits whose emails we most want. We over-fetch slightly and
#: attribute client-side instead (exact login match, else a name match at half
#: confidence) -- see ``enrich_gql.commit_candidates``.
COMMIT_PROBE = """
fragment CommitProbe on User {
  login
  name
  repositories(
    first: $repos
    isFork: false
    privacy: PUBLIC
    ownerAffiliations: [OWNER]
    orderBy: {field: PUSHED_AT, direction: DESC}
  ) {
    nodes {
      nameWithOwner
      defaultBranchRef {
        target {
          __typename
          ... on Commit {
            history(first: $commits) {
              nodes {
                oid
                authoredDate
                author {
                  name
                  email
                  user { login }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def commit_email_query(logins: Sequence[str]) -> tuple[str, dict[str, Any]]:
    """Build one aliased query probing several users at once.

    Batching is what makes commit-email discovery affordable: ``repos=3`` and
    ``commits=5`` over 10 users is ~40 connection requests, i.e. ~1 point for 10
    users. The REST path spends ~4 requests *per user*.

    Logins are passed as GraphQL *variables*, never interpolated into the document.
    """
    if not logins:
        raise ValueError("commit_email_query needs at least one login")

    decls = ", ".join(f"$l{i}: String!" for i in range(len(logins)))
    aliases = "\n".join(
        f"  u{i}: user(login: $l{i}) {{ ...CommitProbe }}" for i in range(len(logins))
    )
    query = (
        f"query CommitEmails($repos: Int!, $commits: Int!, {decls}) {{\n"
        + _RATE_LIMIT
        + "\n"
        + aliases
        + "\n}\n"
        + COMMIT_PROBE
    )
    variables: dict[str, Any] = {f"l{i}": login for i, login in enumerate(logins)}
    return query, variables


#: Alias -> login mapping is positional, so callers can zip results back.
def alias_for(index: int) -> str:
    return f"u{index}"


def profiles_query(
    logins: Sequence[str], include_email: bool = True
) -> tuple[str, dict[str, Any]]:
    """Fetch full profiles for a batch of logins, by alias.

    Used by the events-based `stars` path: the events API hands back only a login, so
    the profile has to be looked up. Batching keeps it at ~1 point per 25 people
    instead of one REST call each.
    """
    if not logins:
        raise ValueError("profiles_query needs at least one login")
    decls = ", ".join(f"$l{i}: String!" for i in range(len(logins)))
    aliases = "\n".join(
        f"  u{i}: user(login: $l{i}) {{ ...UserFields }}" for i in range(len(logins))
    )
    query = (
        f"query Profiles({decls}) {{\n"
        + _RATE_LIMIT
        + "\n"
        + aliases
        + "\n}\n"
        + user_fields(include_email)
    )
    return query, {f"l{i}": login for i, login in enumerate(logins)}


# ------------------------------------------------------------------- discovery

DISCOVER = (
    """
query Discover($q: String!, $first: Int!, $after: String) {
"""
    + _RATE_LIMIT
    + """
  search(query: $q, type: REPOSITORY, first: $first, after: $after) {
    repositoryCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Repository {
        nameWithOwner
        description
        stargazerCount
        forkCount
        isArchived
        isFork
        pushedAt
        primaryLanguage { name }
        repositoryTopics(first: 10) { nodes { topic { name } } }
      }
    }
  }
}
"""
)

DISCOVER_PATH = ("search",)

# -------------------------------------------------------------------- identity

VIEWER = (
    """
query Viewer {
"""
    + _RATE_LIMIT
    + """
  viewer { login }
}
"""
)

#: Cheapest possible shape-check per kind, used by `gitscout doctor`.
PROBE_REPO = ("octocat", "Hello-World")


# ------------------------------------------------------- profile extras

#: Links a person has chosen to publish on their own GitHub profile, plus how many
#: public repositories they own. `socialAccounts` is where GitHub keeps the LinkedIn
#: link people add themselves, which makes it the one legitimate GitHub-side bridge
#: to a LinkedIn-keyed CRM. Both connections need an explicit `first`.
_SOCIAL_FIELDS = """
fragment SocialFields on User {
  login
  repositories(first: 1, privacy: PUBLIC, ownerAffiliations: [OWNER]) { totalCount }
  socialAccounts(first: 10) { nodes { provider url } }
}
"""


def social_query(logins: Sequence[str]) -> tuple[str, dict[str, Any]]:
    """Batch-fetch social links and public repo counts, by alias."""
    if not logins:
        raise ValueError("social_query needs at least one login")
    decls = ", ".join(f"$l{i}: String!" for i in range(len(logins)))
    aliases = "\n".join(
        f"  u{i}: user(login: $l{i}) {{ ...SocialFields }}" for i in range(len(logins))
    )
    query = f"query Social({decls}) {{\n" + _RATE_LIMIT + "\n" + aliases + "\n}\n" + _SOCIAL_FIELDS
    return query, {f"l{i}": login for i, login in enumerate(logins)}


#: One repository's identity and headline numbers, for the repository registry.
#: `databaseId` is the stable identifier: it survives a rename or a transfer, where
#: `nameWithOwner` does not.
REPO_INFO = (
    """
query RepoInfo($owner: String!, $name: String!) {
"""
    + _RATE_LIMIT
    + """
  repository(owner: $owner, name: $name) {
    id
    databaseId
    nameWithOwner
    name
    owner { login }
    url
    description
    homepageUrl
    stargazerCount
    forkCount
    isArchived
    primaryLanguage { name }
    repositoryTopics(first: 10) { nodes { topic { name } } }
  }
}
"""
)
