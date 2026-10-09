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


#: The default (with email), kept as a module constant for the common case.
USER_FIELDS = user_fields(True)
USER_FIELDS_NO_EMAIL = user_fields(False)

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
