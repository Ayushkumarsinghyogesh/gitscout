"""Target profiles and repo discovery."""
from __future__ import annotations

import asyncio

import pytest
from gqlhelpers import connection

from gitscout.models import ALL_KINDS, DEFAULT_KINDS
from gitscout.targets import (
    available_profiles,
    build_search_query,
    discover,
    load_targets,
    resolve_profile,
    targets_from_repos,
    to_toml,
)


def run(coro):
    return asyncio.run(coro)


def write(tmp_path, text, name="t.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_cloud_security_profile_is_well_formed():
    """The shipped profile is the scheduled run's live repository list, so its
    CONTENTS change whenever the team adds or retires a repo. Test what must always
    hold instead: it parses, it names at least one repo, and every entry is a real
    owner/name with known signal kinds and no duplicates. This used to assert a
    23-repo list, and failed CI every time the list was trimmed."""
    targets = load_targets("cloud-security")
    repos = [t.repo for t in targets]

    assert targets, "the scheduled profile must name at least one repository"
    assert len(repos) == len({r.lower() for r in repos}), "a repository is listed twice"
    for target in targets:
        owner, _, name = target.repo.partition("/")
        assert owner and name and "/" not in name, f"not owner/name: {target.repo!r}"
        assert set(target.kinds) <= set(ALL_KINDS), f"unknown kind in {target.repo}"


def test_shipped_profile_is_listed():
    assert any(p.stem == "cloud-security" for p in available_profiles())


def test_per_target_kinds_override_the_default(tmp_path):
    path = write(
        tmp_path,
        """
        name = "t"
        [[target]]
        repo = "o/a"
        [[target]]
        repo = "o/b"
        kinds = ["issues", "prs"]
        weight = 0.5
        note = "high intent only"
        """,
    )
    a, b = load_targets(path)
    # omitting `kinds` means DEFAULT_KINDS -- notably without the restricted `stars`
    assert a.kinds == DEFAULT_KINDS
    assert "stars" not in a.kinds and "stars" in ALL_KINDS
    assert b.kinds == ("issues", "prs")
    assert (b.weight, b.note) == (0.5, "high intent only")


def test_kinds_accept_a_comma_string(tmp_path):
    path = write(tmp_path, 'name="t"\n[[target]]\nrepo="o/a"\nkinds="stars, forks"\n')
    assert load_targets(path)[0].kinds == ("stars", "forks")


def test_repo_urls_are_normalised(tmp_path):
    path = write(tmp_path, 'name="t"\n[[target]]\nrepo="https://github.com/o/a"\n')
    assert load_targets(path)[0].repo == "o/a"


def test_bad_profiles_fail_clearly(tmp_path):
    with pytest.raises(ValueError, match="no \\[\\[target\\]\\]"):
        load_targets(write(tmp_path, 'name = "empty"\n'))
    with pytest.raises(ValueError, match="missing `repo`"):
        load_targets(write(tmp_path, 'name="t"\n[[target]]\nnote="oops"\n'))
    with pytest.raises(ValueError, match="unknown kinds"):
        load_targets(write(tmp_path, 'name="t"\n[[target]]\nrepo="o/a"\nkinds=["likes"]\n'))
    with pytest.raises(ValueError):
        load_targets(write(tmp_path, 'name="t"\n[[target]]\nrepo="not a repo"\n'))


def test_missing_profile_says_where_it_looked():
    with pytest.raises(FileNotFoundError, match="Looked in"):
        resolve_profile("does-not-exist")


def test_targets_from_repos():
    targets = targets_from_repos(["o/a", "github.com/o/b"], ("stars",))
    assert [t.repo for t in targets] == ["o/a", "o/b"]
    assert targets[0].kinds == ("stars",)


# ------------------------------------------------------------------- search


def test_build_search_query_composes_qualifiers():
    q = build_search_query(topic="cloud-security", min_stars=250, language="Python")
    assert "topic:cloud-security" in q
    assert "stars:>=250" in q
    assert "language:Python" in q
    assert "archived:false" in q and "is:public" in q


def test_build_search_query_needs_something_to_search_for():
    with pytest.raises(ValueError, match="at least one of"):
        build_search_query(min_stars=100)


def test_discover_filters_and_maps(gql, make_gql_client):
    gql.add(
        "Discover",
        {
            "search": connection(
                nodes=[
                    {
                        "nameWithOwner": "o/good",
                        "description": "A scanner",
                        "stargazerCount": 900,
                        "forkCount": 10,
                        "isArchived": False,
                        "isFork": False,
                        "pushedAt": "2025-01-01T00:00:00Z",
                        "primaryLanguage": {"name": "Go"},
                        "repositoryTopics": {"nodes": [{"topic": {"name": "security"}}]},
                    },
                    {"nameWithOwner": "o/archived", "isArchived": True},
                    {"nameWithOwner": "o/forked", "isFork": True},
                    {"nameWithOwner": "o/skipme", "isArchived": False, "isFork": False},
                    {"nameWithOwner": None},
                ],
                total=5,
            )
        },
    )

    async def go():
        async with make_gql_client(gql) as client:
            return await discover(client, topic="cloud-security", exclude=["O/SKIPME"])

    rows = run(go())
    assert [r["repo"] for r in rows] == ["o/good"]
    assert rows[0]["language"] == "Go" and rows[0]["topics"] == ["security"]


def test_discover_respects_the_limit(gql, make_gql_client):
    nodes = [
        {"nameWithOwner": f"o/r{i}", "isArchived": False, "isFork": False} for i in range(10)
    ]
    gql.add("Discover", {"search": connection(nodes=nodes)})

    async def go():
        async with make_gql_client(gql) as client:
            return await discover(client, topic="x", limit=3)

    assert len(run(go())) == 3


def test_to_toml_round_trips_through_load_targets(tmp_path):
    rows = [{"repo": "o/a", "description": 'uses "quotes"'}, {"repo": "o/b"}]
    path = write(tmp_path, to_toml(rows, name="gen"), name="gen.toml")
    targets = load_targets(path)
    assert [t.repo for t in targets] == ["o/a", "o/b"]
    assert "quotes" in (targets[0].note or "")
