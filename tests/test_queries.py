"""Static checks on the GraphQL documents.

`gitscout doctor` validates these against live GitHub, but that needs a token. These
tests catch the mistakes that do not need one -- unbalanced braces, a fragment spread
with no definition, an undeclared variable, a lost `direction: DESC` -- so a typo in
[queries.py] fails in CI rather than at 2am in a cron job.
"""
from __future__ import annotations

import re

import pytest

from gitscout.models import ALL_KINDS
from gitscout.queries import (
    DISCOVER,
    DISCOVER_PATH,
    KIND_QUERIES,
    NATURALLY_NEWEST_FIRST,
    PROBE_REPO,
    USER_FIELDS,
    VIEWER,
    alias_for,
    commit_email_query,
)

_SPREAD = re.compile(r"\.\.\.\s*([A-Za-z_]\w*)")
_FRAGMENT_DEF = re.compile(r"fragment\s+([A-Za-z_]\w*)\s+on\s+([A-Za-z_]\w*)")
_VAR_USE = re.compile(r"\$([A-Za-z_]\w*)")
_SIGNATURE = re.compile(r"(?:query|mutation)\s+\w*\s*\(([^)]*)\)", re.S)
_DECL = re.compile(r"\$([A-Za-z_]\w*)\s*:")


def documents():
    """Every document, as (label, text)."""
    out = [(f"kind:{kind}", query) for kind, (query, _) in KIND_QUERIES.items()]
    out.append(("discover", DISCOVER))
    out.append(("viewer", VIEWER))
    out.append(("commit_emails", commit_email_query(["alice", "bob"])[0]))
    return out


@pytest.mark.parametrize("label,text", documents())
def test_braces_and_parens_are_balanced(label, text):
    assert text.count("{") == text.count("}"), f"{label}: unbalanced braces"
    assert text.count("(") == text.count(")"), f"{label}: unbalanced parens"


@pytest.mark.parametrize("label,text", documents())
def test_every_fragment_spread_is_defined(label, text):
    defined = {name for name, _ in _FRAGMENT_DEF.findall(text)}
    for spread in _SPREAD.findall(text):
        if spread.startswith("on"):  # an inline fragment: "... on User"
            continue
        assert spread in defined, f"{label}: spreads ...{spread} but never defines it"


@pytest.mark.parametrize("label,text", documents())
def test_every_variable_used_is_declared_and_vice_versa(label, text):
    signature = _SIGNATURE.search(text)
    declared = set(_DECL.findall(signature.group(1))) if signature else set()
    used = set(_VAR_USE.findall(text))
    assert used - declared == set(), f"{label}: uses undeclared {sorted(used - declared)}"
    assert declared - used == set(), f"{label}: declares unused {sorted(declared - used)}"


@pytest.mark.parametrize("label,text", documents())
def test_every_document_asks_for_its_own_cost(label, text):
    """Without `rateLimit` the client cannot account for points at all."""
    assert "rateLimit" in text
    assert "cost" in text and "remaining" in text


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_interaction_queries_are_newest_first(kind):
    """The whole incremental/cron design depends on newest-first ordering."""
    query, _ = KIND_QUERIES[kind]
    if kind in NATURALLY_NEWEST_FIRST:
        # Commit.history is reverse-chronological by definition and takes no orderBy.
        assert "orderBy" not in query, f"{kind} does not accept orderBy"
        assert "history(" in query
        return
    assert "direction: DESC" in query, f"{kind} must be ordered newest-first"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_interaction_queries_paginate_and_report_totals(kind):
    query, path = KIND_QUERIES[kind]
    assert "$first" in query and "$after" in query
    assert "hasNextPage" in query and "endCursor" in query
    assert "totalCount" in query
    # the connection we paginate is the LAST hop of the path, and it must be a field
    # that actually takes arguments in the document
    assert path[0] == "repository"
    assert f"{path[-1]}(" in query


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_interaction_queries_carry_the_user_fragment(kind):
    """The profile riding along with the interaction is the core cost saving."""
    query, _ = KIND_QUERIES[kind]
    assert "...UserFields" in query
    assert "fragment UserFields on User" in query


def test_user_fragment_requests_the_fields_the_mapper_reads():
    for field in (
        "login", "id", "name", "email", "company", "location", "bio",
        "websiteUrl", "twitterUsername", "isHireable", "createdAt", "__typename",
    ):
        assert field in USER_FIELDS, f"UserFields is missing {field}"
    assert "followers { totalCount }" in USER_FIELDS


def test_every_kind_has_a_query_and_nothing_extra():
    assert set(KIND_QUERIES) == set(ALL_KINDS)


def test_typename_is_requested_wherever_the_author_can_be_a_bot_or_org():
    for kind in ("forks", "issues", "prs", "discussions"):
        query, _ = KIND_QUERIES[kind]
        # an inline fragment is needed because the field is an interface type
        assert "... on User" in query, f"{kind} must narrow the actor to a User"
        assert "__typename" in query


def test_commit_query_is_built_per_batch():
    query, variables = commit_email_query(["a", "b", "c"])
    assert len(variables) == 3
    for i in range(3):
        assert f"{alias_for(i)}: user(login: $l{i})" in query
    assert "fragment CommitProbe on User" in query
    assert "$repos" in query and "$commits" in query
    # no server-side author filter: see the enrich_gql docstring for why
    assert "history(first: $commits)" in query
    assert "author: {id" not in query


def test_commit_query_rejects_an_empty_batch():
    with pytest.raises(ValueError, match="at least one login"):
        commit_email_query([])


def test_commit_probe_reads_only_the_user_s_own_public_repos():
    _, _ = commit_email_query(["a"])
    from gitscout.queries import COMMIT_PROBE

    assert "isFork: false" in COMMIT_PROBE
    assert "ownerAffiliations: [OWNER]" in COMMIT_PROBE
    assert "privacy: PUBLIC" in COMMIT_PROBE
    assert "... on Commit" in COMMIT_PROBE  # target is a GitObject interface


def test_discover_query_shape():
    assert "type: REPOSITORY" in DISCOVER
    assert "... on Repository" in DISCOVER
    assert DISCOVER_PATH == ("search",)
    assert "repositoryCount" in DISCOVER


def test_probe_repo_is_a_real_public_repo_pair():
    owner, name = PROBE_REPO
    assert owner and name and "/" not in owner and "/" not in name
