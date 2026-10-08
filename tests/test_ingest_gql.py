"""GraphQL ingest: inline profiles, cursor backfill, and the incremental early stop."""
from __future__ import annotations

import asyncio

import pytest
from gqlhelpers import (
    authored_body,
    contribs_body,
    forks_body,
    gql_bot,
    gql_org,
    gql_user,
    repo_commit,
    stars_body,
)

from gitscout.ingest_gql import ingest_kind_gql, ingest_repos_gql, profile_from_node

def run(coro):
    return asyncio.run(coro)


def _ingest(gql, make_gql_client, store, kind="stars", **kw):
    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_kind_gql(client, store, "o/r", kind, **kw)

    return run(go())


# ------------------------------------------------------------ profile mapping


def test_profile_from_node_maps_fields_and_blanks_become_none():
    node = gql_user(
        "alice",
        name="Alice A",
        email="alice@acme.dev",
        company="@acme",
        location="Berlin",
        bio="",
        websiteUrl="https://alice.dev",
        twitterUsername="",
        isHireable=True,
        followers=42,
    )
    profile = profile_from_node(node)
    assert profile is not None
    assert (profile.login, profile.name, profile.public_email) == ("alice", "Alice A", "alice@acme.dev")
    assert (profile.company, profile.location, profile.blog) == ("@acme", "Berlin", "https://alice.dev")
    assert (profile.bio, profile.twitter) == (None, None)  # "" is not a value
    assert (profile.hireable, profile.followers) == (True, 42)
    assert profile.node_id and profile.created_at


def test_profile_from_node_rejects_non_users():
    assert profile_from_node(gql_org("acme")) is None
    assert profile_from_node(gql_bot("dependabot")) is None
    assert profile_from_node({"login": None}) is None


# ------------------------------------------------------------------ extraction


def test_stars_records_interactions_profiles_and_profile_emails(gql, make_gql_client, store):
    gql.add(
        "Stars",
        stars_body(
            [
                (gql_user("alice", name="Alice", email="alice@acme.dev"), "2024-06-01T00:00:00Z"),
                (gql_user("bob"), "2024-05-01T00:00:00Z"),
            ],
            total=2,
        ),
    )
    res = _ingest(gql, make_gql_client, store)

    assert (res.fetched, res.new, res.profiles, res.complete) == (2, 2, 2, True)
    assert res.emails_new == 1 and res.total_count == 2
    assert res.points == 1  # 100 fully-profiled users for one point
    assert store.stats()["by_kind"] == {"stars": 2}
    assert store.profiles(["alice"])["alice"].public_email == "alice@acme.dev"
    rows = store.export_rows()
    assert next(r for r in rows if r["login"] == "alice")["email_source"] == "gql_profile"


def test_junk_profile_email_is_not_stored(gql, make_gql_client, store):
    gql.add(
        "Stars",
        stars_body([(gql_user("ghosty", email="1234+ghosty@users.noreply.github.com"), "2024-01-01T00:00:00Z")]),
    )
    res = _ingest(gql, make_gql_client, store)
    assert res.new == 1 and res.emails_new == 0


def test_forks_uses_owner_and_keeps_orgs_as_interactions(gql, make_gql_client, store):
    gql.add(
        "Forks",
        forks_body([(gql_user("carol"), "2024-04-01T00:00:00Z"), (gql_org("acme"), "2024-03-01T00:00:00Z")]),
    )
    res = _ingest(gql, make_gql_client, store, kind="forks")
    assert (res.fetched, res.profiles) == (2, 1)  # the org gets no profile row
    assert store.stats()["by_kind"] == {"forks": 2}


def test_issues_skips_deleted_authors(gql, make_gql_client, store):
    gql.add(
        "Issues",
        authored_body(
            "issues",
            [(gql_user("dave"), "2024-03-01T00:00:00Z"), (None, "2024-02-01T00:00:00Z")],
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="issues")
    assert res.fetched == 1


def test_ghost_login_is_skipped(gql, make_gql_client, store):
    gql.add("Issues", authored_body("issues", [({"__typename": "User", "login": "ghost"}, "2024-01-01T00:00:00Z")]))
    assert _ingest(gql, make_gql_client, store, kind="issues").fetched == 0


def test_prs_are_supported(gql, make_gql_client, store):
    gql.add(
        "PullRequests",
        authored_body("pullRequests", [(gql_user("erin"), "2024-07-01T00:00:00Z")], extra={"merged": True}),
    )
    res = _ingest(gql, make_gql_client, store, kind="prs")
    assert res.new == 1 and store.stats()["by_kind"] == {"prs": 1}


def test_unknown_kind_is_rejected(gql, make_gql_client, store):
    with pytest.raises(ValueError, match="kind must be"):
        _ingest(gql, make_gql_client, store, kind="likes")


def test_missing_repo_yields_nothing(gql, make_gql_client, store):
    res = _ingest(gql, make_gql_client, store)  # nothing scripted -> NOT_FOUND
    assert (res.fetched, res.pages, res.complete) == (0, 0, False)


# ------------------------------------------------------------------- backfill


def test_backfill_saves_cursor_resumes_and_then_skips(gql, make_gql_client, store):
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")], has_next=True, cursor="c1"))
    first = _ingest(gql, make_gql_client, store, max_items=1)
    assert first.new == 1 and not first.complete
    assert store.get_crawl_state("gql:o/r:stars")["cursor"] == "c1"

    # a second run resumes from the saved cursor
    gql.queue.clear()
    gql.add("Stars", stars_body([(gql_user("b"), "2024-05-01T00:00:00Z")]))
    second = _ingest(gql, make_gql_client, store)
    assert second.new == 1 and second.complete
    assert gql.variables_for("Stars")[-1]["after"] == "c1"
    assert store.get_crawl_state("gql:o/r:stars")["done"] is True

    # a third run has nothing to do
    third = _ingest(gql, make_gql_client, store)
    assert third.skipped and third.new == 0


def test_fresh_clears_the_cursor(gql, make_gql_client, store):
    store.set_crawl_state("gql:o/r:stars", cursor="old", done=True)
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")]))
    res = _ingest(gql, make_gql_client, store, fresh=True)
    assert res.new == 1
    assert gql.variables_for("Stars")[0]["after"] is None


def test_high_water_comes_from_page_one_only(gql, make_gql_client, store):
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")], has_next=True, cursor="c1"))
    gql.add("Stars", stars_body([(gql_user("b"), "2024-01-01T00:00:00Z")]))
    _ingest(gql, make_gql_client, store)
    # DESC order: the newest star overall is on page 1, not the last page we read
    assert store.get_crawl_state("gql:o/r:stars")["high_water"] == "2024-06-01T00:00:00Z"


# ---------------------------------------------------------------- incremental


def test_incremental_stops_at_the_high_water_mark(gql, make_gql_client, store):
    store.set_crawl_state("gql:o/r:stars", high_water="2024-05-01T00:00:00Z")
    gql.add(
        "Stars",
        stars_body(
            [
                (gql_user("new2"), "2024-07-01T00:00:00Z"),
                (gql_user("new1"), "2024-06-01T00:00:00Z"),
                (gql_user("seen"), "2024-05-01T00:00:00Z"),  # <- stop here
                (gql_user("older"), "2024-04-01T00:00:00Z"),
            ],
            has_next=True,
            cursor="c1",
        ),
    )
    res = _ingest(gql, make_gql_client, store, incremental=True)

    assert (res.fetched, res.new, res.stopped_early) == (2, 2, True)
    assert res.status == "up to date"
    assert sorted(r["login"] for r in store.export_rows()) == ["new1", "new2"]
    assert len(gql.calls) == 1  # it never asked for page 2
    assert store.get_crawl_state("gql:o/r:stars")["high_water"] == "2024-07-01T00:00:00Z"


def test_incremental_ignores_a_done_backfill_and_starts_at_page_one(gql, make_gql_client, store):
    store.set_crawl_state("gql:o/r:stars", cursor="deep", done=True, high_water="2024-01-01T00:00:00Z")
    gql.add("Stars", stars_body([(gql_user("fresh"), "2024-08-01T00:00:00Z")]))
    res = _ingest(gql, make_gql_client, store, incremental=True)
    assert res.new == 1 and not res.skipped
    assert gql.variables_for("Stars")[0]["after"] is None


def test_incremental_with_no_high_water_takes_everything(gql, make_gql_client, store):
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")]))
    assert _ingest(gql, make_gql_client, store, incremental=True).new == 1


def test_incremental_does_not_move_the_backfill_cursor(gql, make_gql_client, store):
    store.set_crawl_state("gql:o/r:stars", cursor="keepme", high_water="2024-01-01T00:00:00Z")
    gql.add("Stars", stars_body([(gql_user("x"), "2024-09-01T00:00:00Z")], has_next=True, cursor="other"))
    _ingest(gql, make_gql_client, store, incremental=True)
    assert store.get_crawl_state("gql:o/r:stars")["cursor"] == "keepme"


def test_second_incremental_run_is_a_no_op(gql, make_gql_client, store):
    page = stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")])
    gql.add("Stars", page)
    assert _ingest(gql, make_gql_client, store, incremental=True).new == 1
    # the same page again: everything is at or below the high-water mark
    second = _ingest(gql, make_gql_client, store, incremental=True)
    assert (second.new, second.fetched, second.stopped_early) == (0, 0, True)


# -------------------------------------------------------------- multi-repo run


def test_ingest_repos_gql_walks_every_repo_and_kind(gql, make_gql_client, store):
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")]))
    gql.add("Issues", authored_body("issues", [(gql_user("b"), "2024-05-01T00:00:00Z")]))

    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_repos_gql(client, store, ["o/r"], ["stars", "issues"])

    totals = run(go())
    assert len(totals.results) == 2
    assert totals.new == 2 and totals.points == 2
    assert set(gql.ops()) == {"Stars", "Issues"}


# ------------------------------------------------- discussions & contributors


def test_discussions_are_collected(gql, make_gql_client, store):
    gql.add(
        "Discussions",
        authored_body(
            "discussions",
            [(gql_user("frank", email="frank@acme.dev"), "2024-07-01T00:00:00Z"), (None, "2024-06-01T00:00:00Z")],
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="discussions")
    assert (res.fetched, res.new, res.emails_new) == (1, 1, 1)  # the null author is skipped
    assert store.stats()["by_kind"] == {"discussions": 1}


def test_discussions_disabled_on_a_repo_is_not_an_error(gql, make_gql_client, store):
    """GitHub returns an empty connection rather than failing."""
    gql.add("Discussions", authored_body("discussions", []))
    res = _ingest(gql, make_gql_client, store, kind="discussions")
    assert (res.fetched, res.complete) == (0, True)


def test_contribs_capture_the_commit_email_in_the_same_query(gql, make_gql_client, store):
    """The cheapest email in the tool: login, profile and address in one request."""
    gql.add(
        "Contributors",
        contribs_body(
            [
                repo_commit("grace@acme.dev", name="Grace", user=gql_user("grace", name="Grace")),
                repo_commit("grace@acme.dev", name="Grace", user=gql_user("grace", name="Grace"), oid="2"),
            ],
            total=99,
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="contribs")

    assert (res.fetched, res.new, res.emails_new) == (2, 1, 1)  # same person, two commits
    assert res.total_count == 99
    row = next(r for r in store.export_rows() if r["login"] == "grace")
    assert row["email"] == "grace@acme.dev"
    assert row["email_source"] == "gql_contrib"
    assert row["email_confidence"] == 0.85


def test_contribs_skip_commits_not_linked_to_an_account(gql, make_gql_client, store):
    """No login means nothing to file the address under."""
    gql.add("Contributors", contribs_body([repo_commit("anon@nowhere.dev", name="Anon", user=None)]))
    res = _ingest(gql, make_gql_client, store, kind="contribs")
    assert (res.fetched, res.emails_new) == (0, 0)


def test_contribs_keep_bot_signals_but_not_bot_leads(gql, make_gql_client, store):
    """dependabot/renovate are User-typed nodes and dominate real commit logs.

    The interaction is still recorded -- automation committing is a fact about the repo
    -- but the account never reaches a lead export.
    """
    gql.add(
        "Contributors",
        contribs_body(
            [
                repo_commit("x@y.dev", user=gql_user("dependabot[bot]")),
                repo_commit("r@y.dev", user=gql_user("renovate[bot]")),
                repo_commit("real@acme.dev", user=gql_user("human")),
            ]
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="contribs")
    assert res.fetched == 3  # all three commits recorded
    assert res.profiles == 1  # but only the human gets a profile row
    assert [r["login"] for r in store.export_rows()] == ["human"]


def test_contribs_filter_noreply_commit_addresses(gql, make_gql_client, store):
    gql.add(
        "Contributors",
        contribs_body([repo_commit("1+priv@users.noreply.github.com", user=gql_user("priv"))]),
    )
    res = _ingest(gql, make_gql_client, store, kind="contribs")
    assert res.new == 1 and res.emails_new == 0  # still a lead, just no address


def test_contribs_on_an_empty_repo_yield_nothing(gql, make_gql_client, store):
    gql.add("Contributors", contribs_body([], branch=None))
    res = _ingest(gql, make_gql_client, store, kind="contribs")
    assert (res.fetched, res.pages) == (0, 0)


def test_contribs_are_incremental_too(gql, make_gql_client, store):
    """history is newest-first by definition, so the early stop works unchanged."""
    store.set_crawl_state("gql:o/r:contribs", high_water="2024-05-01T00:00:00Z")
    gql.add(
        "Contributors",
        contribs_body(
            [
                repo_commit("new@acme.dev", user=gql_user("newbie"), at="2024-06-01T00:00:00Z"),
                repo_commit("old@acme.dev", user=gql_user("oldtimer"), at="2024-04-01T00:00:00Z"),
            ],
            has_next=True,
            cursor="c1",
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="contribs", incremental=True)
    assert (res.fetched, res.stopped_early) == (1, True)
    assert [r["login"] for r in store.export_rows()] == ["newbie"]


def test_profile_from_node_rejects_bot_suffixed_logins():
    assert profile_from_node(gql_user("dependabot[bot]")) is None
    assert profile_from_node(gql_user("human")) is not None


# ------------------------------------------------------- missing read:user scope


#: The exact error GitHub returns for a token with no scopes (seen in the wild).
SCOPE_ERROR = {
    "message": (
        "Your token has not been granted the required scopes to execute this query. "
        "The 'email' field requires one of the following scopes: "
        "['user:email', 'read:user'], but your token has only been granted the: [''] "
        "scopes. Please modify your token's scopes at: https://github.com/settings/tokens."
    )
}


def test_scope_error_falls_back_to_a_query_without_email(gql, make_gql_client, store):
    """A token without read:user must still collect everyone -- just no profile emails."""
    from gitscout.ingest_gql import EmailScope

    gql.add_errors("Stars", [SCOPE_ERROR])  # first attempt: with `email`
    gql.add("Stars", stars_body([(gql_user("alice", name="Alice"), "2024-06-01T00:00:00Z")]))

    scope = EmailScope()

    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_kind_gql(client, store, "o/r", "stars", scope=scope)

    res = run(go())

    assert res.new == 1 and res.profiles == 1  # the person was still collected
    assert res.emails_new == 0  # but no profile email
    assert scope.downgraded is True and scope.enabled is False
    # the retry really did drop the field
    sent = [c for c in gql.calls if c[0] == "Stars"]
    assert len(sent) == 2
    assert store.profiles(["alice"])["alice"].name == "Alice"


def test_scope_downgrade_is_remembered_across_kinds(gql, make_gql_client, store):
    """The warning fires once, and later kinds skip the doomed first attempt."""
    from gitscout.ingest_gql import EmailScope

    gql.add_errors("Stars", [SCOPE_ERROR])
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")]))
    gql.add("Issues", authored_body("issues", [(gql_user("b"), "2024-05-01T00:00:00Z")]))

    scope = EmailScope()

    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_repos_gql(client, store, ["o/r"], ["stars", "issues"], scope=scope)

    totals = run(go())
    assert totals.new == 2
    assert scope.downgraded is True
    # stars needed two attempts; issues went straight through without email
    assert len([c for c in gql.calls if c[0] == "Stars"]) == 2
    assert len([c for c in gql.calls if c[0] == "Issues"]) == 1


def test_scope_error_without_email_in_the_query_is_fatal(gql, make_gql_client, store):
    """If it still fails with the field removed, the scope was not the problem."""
    from gitscout.graphql import MissingScopeError
    from gitscout.ingest_gql import EmailScope

    for _ in range(4):
        gql.add_errors("Stars", [SCOPE_ERROR])

    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_kind_gql(
                client, store, "o/r", "stars", scope=EmailScope()
            )

    with pytest.raises(MissingScopeError):
        run(go())


def test_scope_fallback_resumes_from_the_saved_cursor(gql, make_gql_client, store):
    """The retry must not re-walk pages already recorded."""
    from gitscout.ingest_gql import EmailScope

    store.set_crawl_state("gql:o/r:stars", cursor="page7")
    gql.add_errors("Stars", [SCOPE_ERROR])
    gql.add("Stars", stars_body([(gql_user("a"), "2024-06-01T00:00:00Z")]))

    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_kind_gql(client, store, "o/r", "stars", scope=EmailScope())

    run(go())
    # both attempts asked to continue from the stored cursor, not from the beginning
    assert [c[1].get("after") for c in gql.calls if c[0] == "Stars"] == ["page7", "page7"]


def test_contribs_still_get_commit_emails_without_the_scope(gql, make_gql_client, store):
    """`read:user` gates User.email only; the git author line is unaffected."""
    from gitscout.ingest_gql import EmailScope

    gql.add_errors("Contributors", [SCOPE_ERROR])
    gql.add(
        "Contributors",
        contribs_body([repo_commit("grace@acme.dev", name="Grace", user=gql_user("grace"))]),
    )

    async def go():
        async with make_gql_client(gql) as client:
            return await ingest_kind_gql(
                client, store, "o/r", "contribs", scope=EmailScope()
            )

    res = run(go())
    assert res.new == 1 and res.emails_new == 1  # email still found
    row = next(r for r in store.export_rows() if r["login"] == "grace")
    assert row["email"] == "grace@acme.dev" and row["email_source"] == "gql_contrib"


# ------------------------------------------------- automation accounts (bots)


def test_machine_user_bots_are_recorded_but_never_exported(gql, make_gql_client, store):
    """`prowler-bot` reports __typename "User", so the type check alone missed it."""
    gql.add(
        "Stars",
        stars_body(
            [
                (gql_user("prowler-bot", name="Prowler Bot", email="bot@prowler.com"), "2026-09-01T00:00:00Z"),
                (gql_user("pedrooot", name="Pedro", email="pedro@acme.dev"), "2026-08-01T00:00:00Z"),
            ]
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="stars")

    assert res.new == 2  # both interactions kept: a bot starring is still a fact
    assert res.emails_new == 1  # but no bot address
    assert [r["login"] for r in store.export_rows()] == ["pedrooot"]


def test_bot_contribs_keep_the_signal_but_drop_the_commit_email(gql, make_gql_client, store):
    gql.add(
        "Contributors",
        contribs_body(
            [
                repo_commit("bot@prowler.com", name="Prowler Bot", user=gql_user("prowler-bot")),
                repo_commit("real@acme.dev", name="Real Person", user=gql_user("realdev")),
            ]
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="contribs")
    assert res.new == 2 and res.emails_new == 1
    assert [r["login"] for r in store.export_rows()] == ["realdev"]


def test_bot_detection_covers_the_common_shapes_without_false_positives():
    from gitscout.ingest_gql import looks_like_bot

    for bot in (
        "dependabot[bot]", "renovate[bot]", "prowler-bot", "renovate-bot",
        "some_bot", "dependabot", "github-actions", "web-flow", "PROWLER-BOT",
    ):
        assert looks_like_bot(bot), bot
    # real logins that merely contain the letters "bot"
    for person in ("abbot", "talbot", "robotnik", "bottomley", "pedrooot", "Alan-TheGentleman"):
        assert not looks_like_bot(person), person
    assert not looks_like_bot(None) and not looks_like_bot("")


# ------------------------------------------------------- automation accounts


def test_machine_user_bots_are_detected_by_login():
    """Seen in real output: `prowler-bot` has __typename User, so type alone misses it."""
    from gitscout.ingest_gql import looks_like_bot

    for bot in (
        "prowler-bot",
        "dependabot[bot]",
        "renovate-bot",
        "some_bot",
        "dependabot",
        "github-actions",
        "web-flow",
        "PROWLER-BOT",
    ):
        assert looks_like_bot(bot), bot

    # real logins that merely contain "bot" must survive
    for person in ("abbot", "talbot", "robotnik", "botanist", "pedrooot", "Alan-TheGentleman"):
        assert not looks_like_bot(person), person
    assert not looks_like_bot(None) and not looks_like_bot("")


def test_machine_user_bot_is_recorded_but_never_exported(gql, make_gql_client, store):
    gql.add(
        "Stars",
        stars_body(
            [
                (gql_user("prowler-bot", name="Prowler Bot", email="bot@prowler.com"), "2026-09-01T00:00:00Z"),
                (gql_user("realperson", email="real@acme.dev"), "2026-08-01T00:00:00Z"),
            ]
        ),
    )
    res = _ingest(gql, make_gql_client, store)

    assert res.new == 2  # the signal is kept: a bot starring is still a fact
    assert res.emails_new == 1  # but no address is taken from it
    assert [r["login"] for r in store.export_rows()] == ["realperson"]
    assert store.read_query("SELECT user_type FROM interactions WHERE login='prowler-bot'") == [
        {"user_type": "Bot"}
    ]


def test_bot_commit_address_is_not_collected(gql, make_gql_client, store):
    gql.add(
        "Contributors",
        contribs_body(
            [
                repo_commit("bot@prowler.com", name="Prowler Bot", user=gql_user("prowler-bot")),
                repo_commit("real@acme.dev", name="Real", user=gql_user("realdev")),
            ]
        ),
    )
    res = _ingest(gql, make_gql_client, store, kind="contribs")
    assert res.emails_new == 1
    assert [r["login"] for r in store.export_rows()] == ["realdev"]
