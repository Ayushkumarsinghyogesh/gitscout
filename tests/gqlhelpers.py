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
        self.queue: dict[str, list[tuple[int, dict[str, Any] | str]]] = {}
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.rest: dict[str, Any] = {}
        self.rest_calls: list[tuple[str, dict[str, str]]] = []
        self.headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}

    def add_rest(self, path: str, body: Any) -> "FakeGraphQL":
        """Script a REST path (used by the events-based `stars` collector)."""
        self.rest[path] = body
        return self

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

    def add_raw(self, op: str, text: str, status: int = 200) -> "FakeGraphQL":
        """Queue a response body sent as-is -- e.g. JSON cut off mid-read."""
        self.queue.setdefault(op, []).append((status, text))
        return self

    def _with_limit(self, data: dict[str, Any], **kw: Any) -> dict[str, Any]:
        out = dict(data)
        out.setdefault("rateLimit", {**DEFAULT_RATE_LIMIT, **kw})
        return out

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]

    def variables_for(self, op: str) -> list[dict[str, Any]]:
        return [c[1] for c in self.calls if c[0] == op]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            # Tests share one transport for both APIs; a REST GET (e.g. the events
            # endpoint) gets an empty list unless `rest` is scripted for that path.
            body = self.rest.get(request.url.path, [])
            self.rest_calls.append((request.url.path, dict(request.url.params)))
            return httpx.Response(200, json=body, headers=self.headers)
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
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=self.headers)
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


def watch_event(login: str, at: str = "2026-10-01T00:00:00Z") -> dict[str, Any]:
    """A WatchEvent -- GitHub's name for 'someone starred this repo'."""
    return {"type": "WatchEvent", "created_at": at, "actor": {"login": login}}



def comment(
    comment_id: str,
    author: dict[str, Any] | None,
    *,
    body: str = "a comment",
    at: str = "2026-01-02T00:00:00Z",
    association: str = "NONE",
    is_answer: bool | None = None,
    upvotes: int | None = None,
    replies: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """An IssueComment or DiscussionComment node."""
    node: dict[str, Any] = {
        "id": comment_id,
        "url": f"https://github.com/o/r/comments/{comment_id}",
        "createdAt": at,
        "updatedAt": at,
        "authorAssociation": association,
        "body": body,
        "author": author,
    }
    if is_answer is not None:
        node["isAnswer"] = is_answer
    if upvotes is not None:
        node["upvoteCount"] = upvotes
    if replies:
        node["replies"] = connection(nodes=list(replies))
    return node


def review_comment(comment_id: str, author, *, review_id: str, path: str = "pkg/scan.go",
                   body: str = "nit: rename this", at: str = "2026-01-04T01:00:00Z"):
    """An inline code-review comment, as reviewThreads returns it."""
    return {
        "id": comment_id,
        "url": f"https://github.com/o/r/pull/1#discussion_{comment_id}",
        "createdAt": at,
        "updatedAt": at,
        "authorAssociation": "MEMBER",
        "body": body,
        "path": path,
        "pullRequestReview": {"id": review_id},
        "author": author,
    }


def review(
    review_id: str,
    author: dict[str, Any] | None,
    *,
    state: str = "APPROVED",
    body: str = "",
    at: str = "2026-01-04T00:00:00Z",
    association: str = "MEMBER",
) -> dict[str, Any]:
    """A PullRequestReview node -- a verdict, with or without a body."""
    return {
        "id": review_id,
        "url": f"https://github.com/o/r/reviews/{review_id}",
        "state": state,
        "body": body,
        "submittedAt": at,
        "updatedAt": at,
        "authorAssociation": association,
        "author": author,
    }


def _thread_common(
    number: int,
    author: dict[str, Any] | None,
    *,
    title: str | None,
    body: str,
    created: str,
    updated: str | None,
    association: str,
    labels: Sequence[str],
    reactions: int,
    comments: Sequence[dict[str, Any]],
    comments_total: int | None,
) -> dict[str, Any]:
    return {
        "id": f"T_{number}",
        "number": number,
        "url": f"https://github.com/o/r/issues/{number}",
        "title": title if title is not None else f"Thread {number}",
        "body": body,
        "createdAt": created,
        "updatedAt": updated or created,
        "locked": False,
        "authorAssociation": association,
        "author": author,
        "reactions": {"totalCount": reactions},
        "labels": {"totalCount": len(labels), "nodes": [{"name": n} for n in labels]},
        "thread": connection(
            nodes=list(comments),
            total=comments_total if comments_total is not None else len(comments),
        ),
    }


def issue_node(
    number: int,
    author: dict[str, Any] | None,
    *,
    title: str | None = None,
    body: str = "steps to reproduce",
    state: str = "OPEN",
    state_reason: str | None = None,
    created: str = "2026-01-01T00:00:00Z",
    updated: str | None = None,
    closed: str | None = None,
    association: str = "FIRST_TIME_CONTRIBUTOR",
    labels: Sequence[str] = ("bug",),
    assignees: Sequence[str] = (),
    participants: Sequence[str] = (),
    milestone: str | None = None,
    reactions: int = 0,
    comments: Sequence[dict[str, Any]] = (),
    comments_total: int | None = None,
) -> dict[str, Any]:
    """One node of the `IssueDetails` connection."""
    node = _thread_common(
        number, author, title=title, body=body, created=created, updated=updated,
        association=association, labels=labels, reactions=reactions,
        comments=comments, comments_total=comments_total,
    )
    node.update(
        {
            "state": state,
            "stateReason": state_reason,
            "closedAt": closed,
            "assignees": {"nodes": [{"login": n} for n in assignees]},
            "milestone": {"title": milestone} if milestone else None,
            "participants": {
                "totalCount": len(participants),
                "nodes": [{"login": n} for n in participants],
            },
        }
    )
    return node


def pr_node(
    number: int,
    author: dict[str, Any] | None,
    *,
    state: str = "MERGED",
    merged: bool = True,
    merged_at: str | None = "2026-01-05T00:00:00Z",
    merged_by: str | None = "maintainer",
    is_draft: bool = False,
    additions: int = 42,
    deletions: int = 7,
    changed_files: int = 3,
    commits: int = 2,
    base_ref: str = "master",
    head_ref: str = "fix/thing",
    review_decision: str | None = "APPROVED",
    reviews: Sequence[dict[str, Any]] = (),
    reviews_has_next: bool = False,
    reviews_cursor: str | None = None,
    review_thread_comments: Sequence[dict[str, Any]] = (),
    **kw: Any,
) -> dict[str, Any]:
    """One node of the `PullRequestDetails` connection."""
    node = issue_node(number, author, **kw)
    node.pop("stateReason", None)
    node.update(
        {
            "state": state,
            "merged": merged,
            "mergedAt": merged_at,
            "mergedBy": {"login": merged_by} if merged_by else None,
            "isDraft": is_draft,
            "additions": additions,
            "deletions": deletions,
            "changedFiles": changed_files,
            "commits": {"totalCount": commits},
            "baseRefName": base_ref,
            "headRefName": head_ref,
            "reviewDecision": review_decision,
            "reviews": connection(nodes=list(reviews), has_next=reviews_has_next,
                                  cursor=reviews_cursor),
            "reviewThreads": connection(
                nodes=[{"comments": connection(nodes=list(review_thread_comments))}]
                if review_thread_comments else []
            ),
        }
    )
    return node


def discussion_node(
    number: int,
    author: dict[str, Any] | None,
    *,
    category: str = "Q&A",
    upvotes: int = 3,
    answer_author: dict[str, Any] | None = None,
    answer_at: str = "2026-01-06T00:00:00Z",
    **kw: Any,
) -> dict[str, Any]:
    """One node of the `DiscussionDetails` connection."""
    kw.setdefault("association", "NONE")
    node = _thread_common(
        number,
        author,
        title=kw.pop("title", None),
        body=kw.pop("body", "how do I configure this?"),
        created=kw.pop("created", "2026-01-01T00:00:00Z"),
        updated=kw.pop("updated", None),
        association=kw.pop("association"),
        labels=kw.pop("labels", ()),
        reactions=kw.pop("reactions", 0),
        comments=kw.pop("comments", ()),
        comments_total=kw.pop("comments_total", None),
    )
    node.update(
        {
            "lastEditedAt": None,
            "upvoteCount": upvotes,
            "category": {"name": category},
            "answer": (
                {"createdAt": answer_at, "author": answer_author} if answer_author else None
            ),
        }
    )
    return node


def thread_body(
    field: str,
    nodes: Sequence[dict[str, Any]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """`repository.<field>` for one of the thread-detail documents."""
    return {
        "repository": {
            "nameWithOwner": "o/r",
            field: connection(
                nodes=list(nodes), has_next=has_next, cursor=cursor, total=total
            ),
        }
    }


def tail_body(
    field: str, number: int, comments: Sequence[dict[str, Any]], *, total: int | None = None
) -> dict[str, Any]:
    """`repository.<issue|pullRequest|discussion>.comments` -- a tail query response."""
    return {
        "repository": {
            field: {
                "number": number,
                "comments": {
                    "totalCount": total if total is not None else len(comments),
                    "nodes": list(comments),
                },
            }
        }
    }
