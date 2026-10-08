"""Stage 1: pull stargazers / forkers / issue authors of a repo into the interactions table."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .github_client import GitHubClient
from .models import KINDS, Interaction
from .storage import Store

log = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", re.I)
_SKIP_LOGINS = {"ghost"}


def parse_repo(value: str) -> str:
    """Accept 'owner/name' or a github.com URL; return canonical 'owner/name'."""
    value = value.strip()
    m = _URL_RE.match(value)
    if m:
        owner, name = m.group(1), m.group(2)
        name = name[:-4] if name.endswith(".git") else name
        value = f"{owner}/{name}"
    if not _REPO_RE.match(value):
        raise ValueError(f"Not a valid repo (expected owner/name or GitHub URL): {value!r}")
    return value


# ---- per-kind extractors: API item -> Interaction (or None to skip) -----------------


def _star(repo: str, item: Mapping[str, Any]) -> Interaction | None:
    user = item.get("user") or {}
    login = user.get("login")
    if not login:
        return None
    return Interaction(repo, login, "stars", user.get("type"), item.get("starred_at"))


def _fork(repo: str, item: Mapping[str, Any]) -> Interaction | None:
    owner = item.get("owner") or {}
    login = owner.get("login")
    if not login:
        return None
    return Interaction(
        repo, login, "forks", owner.get("type"), item.get("created_at"), item.get("full_name")
    )


def _issue(repo: str, item: Mapping[str, Any]) -> Interaction | None:
    if "pull_request" in item:  # the issues endpoint also returns PRs
        return None
    user = item.get("user") or {}
    login = user.get("login")
    if not login:
        return None
    return Interaction(
        repo, login, "issues", user.get("type"), item.get("created_at"), item.get("html_url")
    )


@dataclass(frozen=True)
class _Spec:
    path: str
    params: dict[str, Any]
    headers: dict[str, str] | None
    extract: Callable[[str, Mapping[str, Any]], Interaction | None]


SPECS: dict[str, _Spec] = {
    "stars": _Spec(
        "/repos/{repo}/stargazers",
        {"per_page": 100},
        {"Accept": "application/vnd.github.star+json"},  # adds starred_at
        _star,
    ),
    "forks": _Spec("/repos/{repo}/forks", {"per_page": 100, "sort": "oldest"}, None, _fork),
    "issues": _Spec(
        "/repos/{repo}/issues",
        {"per_page": 100, "state": "all", "sort": "created", "direction": "asc"},
        None,
        _issue,
    ),
}
assert set(SPECS) == set(KINDS)


@dataclass
class IngestResult:
    repo: str
    kind: str
    fetched: int = 0
    new: int = 0
    complete: bool = False
    skipped: bool = False


async def ingest_kind(
    client: GitHubClient,
    store: Store,
    repo: str,
    kind: str,
    *,
    max_items: int = 0,
    fresh: bool = False,
) -> IngestResult:
    """Crawl one (repo, kind). Resumable: a checkpoint is saved after every page.

    max_items is a soft cap per run (0 = unlimited), rounded up to a whole page (100).
    Re-running resumes from the checkpoint; use fresh=True to start over (e.g. to pick up
    new stars after a completed crawl).
    """
    spec = SPECS[kind]
    key = f"{repo}:{kind}"
    result = IngestResult(repo, kind)

    if fresh:
        store.clear_checkpoint(key)
    cp = store.get_checkpoint(key)
    if cp and cp[1]:
        log.info("%s already fully ingested (use --fresh to redo)", key)
        result.complete = result.skipped = True
        return result
    start_url = cp[0] if cp else None

    async for items, next_url in client.pages(
        spec.path.format(repo=repo), spec.params, spec.headers, start_url=start_url
    ):
        rows = [r for r in (spec.extract(repo, it) for it in items) if r and r.login not in _SKIP_LOGINS]
        result.fetched += len(rows)
        result.new += store.add_interactions(rows)
        store.set_checkpoint(key, next_url, done=next_url is None)
        log.info("%s: +%d (%d new so far)", key, len(rows), result.new)
        if next_url is None:
            result.complete = True
        elif max_items and result.fetched >= max_items:
            break

    if not result.complete and store.get_checkpoint(key) is None:
        # repo missing / private / blocked: nothing was fetched
        log.warning("%s: no data (repo not found or not accessible)", key)
    return result
