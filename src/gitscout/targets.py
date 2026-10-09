"""Target profiles: which repos to watch, and finding more of them.

A profile is a TOML file (stdlib ``tomllib``, no dependency) listing repos and the
signals to collect from each. The shipped one is ``targets/cloud-security.toml``.

``discover`` uses the GraphQL search API to find more repos like them, so the target
list can grow without you hand-maintaining it.
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Iterable, Sequence

from .graphql import GraphQLClient
from .ingest import parse_repo
from .models import ALL_KINDS, DEFAULT_KINDS, Target
from .queries import DISCOVER, DISCOVER_PATH

log = logging.getLogger(__name__)

#: Shipped profiles live next to the package, or in the repo root during development.
_SEARCH_DIRS = (
    Path(__file__).resolve().parent / "profiles",
    Path(__file__).resolve().parents[2] / "targets",
    Path("targets"),
)


def _coerce_kinds(raw: Any) -> tuple[str, ...]:
    """Kinds for one target entry. Omitting `kinds` means DEFAULT_KINDS, not ALL_KINDS.

    The difference matters: ALL_KINDS still contains `stars`, which GitHub restricts to
    repo admins, so defaulting to it would spend a wasted query per repo per run. A
    profile can still ask for `stars` explicitly, for repos you administer.
    """
    if raw is None:
        return DEFAULT_KINDS
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    kinds = tuple(dict.fromkeys(str(k).strip().lower() for k in items if str(k).strip()))
    bad = [k for k in kinds if k not in ALL_KINDS]
    if bad:
        raise ValueError(f"unknown kinds {bad} (expected any of {', '.join(ALL_KINDS)})")
    return kinds or DEFAULT_KINDS


def load_targets(path: str | Path) -> list[Target]:
    """Read a target profile. Accepts a path or the bare name of a shipped profile."""
    resolved = resolve_profile(path)
    with resolved.open("rb") as fh:
        data = tomllib.load(fh)

    entries = data.get("target") or []
    if not entries:
        raise ValueError(f"{resolved} contains no [[target]] entries")

    out: list[Target] = []
    for entry in entries:
        repo = entry.get("repo")
        if not repo:
            raise ValueError(f"{resolved}: a [[target]] entry is missing `repo`")
        out.append(
            Target(
                repo=parse_repo(str(repo)),
                kinds=_coerce_kinds(entry.get("kinds")),
                weight=float(entry.get("weight", 1.0)),
                note=entry.get("note"),
            )
        )
    return out


def resolve_profile(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    stem = candidate.stem if candidate.suffix else candidate.name
    for directory in _SEARCH_DIRS:
        for name in (f"{stem}.toml", str(candidate)):
            option = directory / name
            if option.is_file():
                return option
    raise FileNotFoundError(
        f"No target profile at {path!r}. Looked in: "
        + ", ".join(str(d) for d in _SEARCH_DIRS)
    )


def available_profiles() -> list[Path]:
    seen: dict[str, Path] = {}
    for directory in _SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.toml")):
            seen.setdefault(file.stem, file)
    return list(seen.values())


def targets_from_repos(repos: Iterable[str], kinds: Sequence[str]) -> list[Target]:
    return [Target(repo=parse_repo(r), kinds=tuple(kinds)) for r in repos]


# ------------------------------------------------------------------- discovery


def build_search_query(
    *,
    topic: str | None = None,
    text: str | None = None,
    min_stars: int = 500,
    language: str | None = None,
    pushed_since: str | None = None,
) -> str:
    """Compose a GitHub search qualifier string."""
    parts: list[str] = []
    if text:
        parts.append(text)
    if topic:
        parts.append(f"topic:{topic}")
    if language:
        parts.append(f"language:{language}")
    if min_stars:
        parts.append(f"stars:>={min_stars}")
    if pushed_since:
        parts.append(f"pushed:>={pushed_since}")
    parts.append("archived:false")
    parts.append("is:public")
    if not (text or topic or language):
        raise ValueError("discover needs at least one of: topic, text, language")
    return " ".join(parts)


async def discover(
    client: GraphQLClient,
    *,
    topic: str | None = None,
    text: str | None = None,
    min_stars: int = 500,
    language: str | None = None,
    pushed_since: str | None = None,
    limit: int = 50,
    exclude: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Search GitHub for repos worth adding to a target profile."""
    query = build_search_query(
        topic=topic, text=text, min_stars=min_stars, language=language, pushed_since=pushed_since
    )
    skip = {r.lower() for r in exclude}
    found: list[dict[str, Any]] = []

    pages = client.paginate(
        DISCOVER,
        {"q": query},
        DISCOVER_PATH,
        page_size=min(100, max(1, limit)),
        max_pages=max(1, (limit + 99) // 100),
    )
    async for page in pages:
        for node in page.nodes:
            name = node.get("nameWithOwner")
            if not name or name.lower() in skip or node.get("isArchived") or node.get("isFork"):
                continue
            topics = [
                (t.get("topic") or {}).get("name")
                for t in ((node.get("repositoryTopics") or {}).get("nodes") or [])
            ]
            found.append(
                {
                    "repo": name,
                    "stars": node.get("stargazerCount"),
                    "forks": node.get("forkCount"),
                    "language": (node.get("primaryLanguage") or {}).get("name"),
                    "description": node.get("description"),
                    "pushed_at": node.get("pushedAt"),
                    "topics": [t for t in topics if t],
                }
            )
            if len(found) >= limit:
                await pages.aclose()
                return found
    return found


def to_toml(rows: Sequence[dict[str, Any]], *, name: str = "discovered") -> str:
    """Render discovered repos as a target profile you can save and edit."""
    lines = [f'name = "{name}"', 'description = "Generated by gitscout discover"', ""]
    for row in rows:
        lines.append("[[target]]")
        lines.append(f'repo = "{row["repo"]}"')
        note = (row.get("description") or "").replace('"', "'").strip()
        if note:
            lines.append(f'note = "{note[:120]}"')
        lines.append("")
    return "\n".join(lines)
