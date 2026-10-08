"""Tiny builders for GitHub-API-shaped JSON."""
from __future__ import annotations


def user(login: str, type_: str = "User") -> dict:
    return {"login": login, "type": type_}


def star(login: str, at: str = "2024-01-01T00:00:00Z", type_: str = "User") -> dict:
    return {"starred_at": at, "user": user(login, type_)}


def fork(owner: str, repo: str = "r", at: str = "2024-02-01T00:00:00Z") -> dict:
    return {"full_name": f"{owner}/{repo}", "created_at": at, "owner": user(owner)}


def issue(login: str, number: int = 1, pr: bool = False) -> dict:
    item = {
        "number": number,
        "created_at": "2024-03-01T00:00:00Z",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "user": user(login),
    }
    if pr:
        item["pull_request"] = {"url": "x"}
    return item


def profile(login: str, **kw) -> dict:
    base = {"login": login, "type": "User", "name": None, "email": None, "blog": "", "followers": 0}
    base.update(kw)
    return base
