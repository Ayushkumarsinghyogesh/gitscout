"""Builders for GitHub GraphQL-shaped responses, plus an in-process fake endpoint."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

import httpx

_OP_RE = re.compile(r"\b(?:query|mutation)\s+(\w+)")

#: Mirrors the real `rateLimit` block so cost accounting is exercised.
DEFAULT_RATE_LIMIT = {
    "cost": 1,
    "remaining": 4999,
    "resetAt": "2030-01-01T00:00:00Z",
    "limit": 5000,
    "nodeCount": 100,
}


def op_name(query: str) -> str:
    match = _OP_RE.search(query or "")
    return match.group(1) if match else "anonymous"


# ------------------------------------------------------------------- node shapes


def gql_user(login: str, **kw: Any) -> dict[str, Any]:
    """A UserFields fragment. GraphQL sends "" rather than null for unset strings."""
    node = {
        "__typename": "User",
        "login": login,
        "id": f"MDQ6VXNlcj{login}",
        "databaseId": abs(hash(login)) % 10**7,
        "name": "",
        "email": "",
        "company": "",
        "location": "",
        "bio": "",
        "websiteUrl": "",
        "twitterUsername": "",
        "isHireable": False,
        "createdAt": "2015-01-01T00:00:00Z",
        "followers": {"totalCount": 0},
    }
    followers = kw.pop("followers", None)
    if followers is not None:
        node["followers"] = {"totalCount": followers}
    node.update(kw)
    return node


def gql_org(login: str) -> dict[str, Any]:
    return {"__typename": "Organization", "login": login}


def gql_bot(login: str) -> dict[str, Any]:
    return {"__typename": "Bot", "login": login}


def connection(
    *,
    nodes: Sequence[Any] | None = None,
    edges: Sequence[Any] | None = None,
    has_next: bool = False,
    cursor: str | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    conn: dict[str, Any] = {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "totalCount": total if total is not None else len(nodes or edges or []),
    }
    if edges is not None:
        conn["edges"] = list(edges)
    else:
        conn["nodes"] = list(nodes or [])
    return conn


def stars_body(
    pairs: Iterable[tuple[dict[str, Any], str]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """`pairs` is (user node, starredAt)."""
    edges = [{"starredAt": at, "node": node} for node, at in pairs]
    return {
        "repository": {
            "nameWithOwner": "o/r",
            "stargazers": connection(edges=edges, has_next=has_next, cursor=cursor, total=total),
        }
    }


def forks_body(
    owners: Iterable[tuple[dict[str, Any], str]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    nodes = [
        {
            "nameWithOwner": f"{owner.get('login')}/r",
            "createdAt": at,
            "owner": owner,
        }
        for owner, at in owners
    ]
    return {
        "repository": {
            "nameWithOwner": "o/r",
            "forks": connection(nodes=nodes, has_next=has_next, cursor=cursor),
        }
    }


def authored_body(
    field: str,
    authors: Iterable[tuple[dict[str, Any] | None, str]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = []
    for index, (author, at) in enumerate(authors, start=1):
        node = {
            "number": index,
            "url": f"https://github.com/o/r/issues/{index}",
            "createdAt": at,
            "author": author,
        }
        node.update(extra or {})
        nodes.append(node)
    return {
        "repository": {
            "nameWithOwner": "o/r",
            field: connection(nodes=nodes, has_next=has_next, cursor=cursor),
        }
    }


def contribs_body(
    commits: Iterable[dict[str, Any]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    total: int | None = None,
    branch: dict | None = ...,
) -> dict[str, Any]:
    """repository.defaultBranchRef.target.history -- `branch=None` is an empty repo."""
    if branch is None:
        return {"repository": {"nameWithOwner": "o/r", "defaultBranchRef": None}}
    return {
        "repository": {
            "nameWithOwner": "o/r",
            "defaultBranchRef": {
                "name": "main",
                "target": {
                    "__typename": "Commit",
                    "history": connection(
                        nodes=list(commits), has_next=has_next, cursor=cursor, total=total
                    ),
                },
            },
        }
    }


def commit(email: str, *, name: str | None = None, linked: str | None = None) -> dict[str, Any]:
    return {
        "oid": "abc123",
        "authoredDate": "2024-05-01T00:00:00Z",
        "author": {
            "name": name,
            "email": email,
            "user": {"login": linked} if linked else None,
        },
    }


def commit_probe(login: str, *, name: str | None = None, repos: Sequence[Sequence[dict]] = ()):
    """One `u<N>` alias payload: a user with repos, each holding commits."""
    return {
        "login": login,
        "name": name,
        "repositories": {
            "nodes": [
                {
                    "nameWithOwner": f"{login}/repo{i}",
                    "defaultBranchRef": {
                        "target": {"__typename": "Commit", "history": {"nodes": list(commits)}}
                    },
                }
                for i, commits in enumerate(repos)
            ]
        },
    }


def error(message: str, *, type_: str | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"message": message}
    if type_:
        err["type"] = type_
    return err


# -------------------------------------------------------------- fake endpoint


class FakeGraphQL:
    """Scripted GraphQL endpoint. Responses are queued per operation name."""

    def __init__(self) -> None:
        self.queue: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}

    def add(self, op: str, data: dict[str, Any] | None = None, **kw: Any) -> "FakeGraphQL":
        """Queue one successful response for `op`."""
        return self.add_body(op, {"data": self._with_limit(data or {}, **kw)})

    def add_pages(self, op: str, bodies: Sequence[dict[str, Any]]) -> "FakeGraphQL":
        for body in bodies:
            self.add(op, body)
        return self

    def add_errors(
        self,
        op: str,
        errors: Sequence[dict[str, Any]],
        data: dict[str, Any] | None = None,
        status: int = 200,
    ) -> "FakeGraphQL":
        body: dict[str, Any] = {"errors": list(errors)}
        if data is not None:
            body["data"] = self._with_limit(data)
        return self.add_body(op, body, status=status)

    def add_body(self, op: str, body: dict[str, Any], status: int = 200) -> "FakeGraphQL":
        self.queue.setdefault(op, []).append((status, body))
        return self

    def add_status(self, op: str, status: int, text: str = "boom") -> "FakeGraphQL":
        return self.add_body(op, {"message": text}, status=status)

    def _with_limit(self, data: dict[str, Any], **kw: Any) -> dict[str, Any]:
        out = dict(data)
        out.setdefault("rateLimit", {**DEFAULT_RATE_LIMIT, **kw})
        return out

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]

    def variables_for(self, op: str) -> list[dict[str, Any]]:
        return [c[1] for c in self.calls if c[0] == op]

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        query = payload.get("query", "")
        name = op_name(query)
        self.calls.append((name, payload.get("variables") or {}, dict(request.headers)))

        pending = self.queue.get(name)
        if not pending:
            # Unscripted operation: behave like a repo that does not exist.
            return httpx.Response(
                200,
                json={"data": {"rateLimit": DEFAULT_RATE_LIMIT}, "errors": [error("Could not resolve", type_="NOT_FOUND")]},
                headers=self.headers,
            )
        status, body = pending.pop(0) if len(pending) > 1 else pending[0]
        return httpx.Response(status, json=body, headers=self.headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def repo_commit(
    email: str,
    *,
    name: str | None = None,
    user: dict[str, Any] | None = None,
    oid: str = "deadbeef",
    at: str = "2024-05-01T00:00:00Z",
) -> dict[str, Any]:
    """A commit node as the Contributors query shapes it: author.user is UserFields."""
    return {
        "oid": oid,
        "authoredDate": at,
        "author": {"name": name, "email": email, "user": user},
    }
