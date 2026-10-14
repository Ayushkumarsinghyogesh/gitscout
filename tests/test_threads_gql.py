"""Thread harvesting: issues, PRs and discussions -- mapping, incremental, exports.

`interactions` is keyed (repo, login, kind), so it can only record *that* someone
files issues. These tests cover the stage that records *which* threads, and every
direction of involvement: opened it, commented on it, reviewed it, answered it.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from gqlhelpers import (
    comment,
    connection,
    discussion_node,
    gql_bot,
    gql_user,
    issue_node,
    pr_node,
    review,
    tail_body,
    thread_body,
)

from gitscout.export import (
    COLUMNS,
    COMMENT_COLUMNS,
    THREAD_COLUMNS,
    write_export,
    write_thread_comments,
    write_threads,
)
from gitscout.models import (
    EmailCandidate,
    Interaction,
    Profile,
    ThreadComment,
    ThreadRecord,
)
from gitscout.threads_gql import fill_capped_threads, harvest_threads, thread_from_node

CONNECTION = {"issues": "issues", "prs": "pullRequests", "discussions": "discussions"}
OPERATION = {"issues": "IssueDetails", "prs": "PullRequestDetails", "discussions": "DiscussionDetails"}


def run(coro):
    return asyncio.run(coro)


def _harvest(gql, make_gql_client, store, kind="issues", repo="o/r", **kw):
    async def go():
        async with make_gql_client(gql) as client:
            return await harvest_threads(client, store, repo, kind, **kw)

    return run(go())


# ------------------------------------------------------------ node -> record


def test_an_issue_maps_every_field():
    node = issue_node(
        12,
        gql_user("alice"),
        title="Scan crashes on GovCloud",
        body="Traceback follows",
        state="CLOSED",
        state_reason="COMPLETED",
        created="2026-01-01T00:00:00Z",
        updated="2026-03-01T00:00:00Z",
        closed="2026-02-01T00:00:00Z",
        labels=("bug", "provider/aws"),
        assignees=("maintainer",),
        participants=("alice", "maintainer"),
        milestone="v5.1",
        reactions=4,
        comments=[
            comment("c1", gql_user("maintainer"), body="which region?"),
            comment("c2", gql_user("alice"), body="us-gov-west-1"),
        ],
    )

    record, comments = thread_from_node("o/r", "issues", node)

    assert (record.repo, record.kind, record.number) == ("o/r", "issues", 12)
    assert record.title == "Scan crashes on GovCloud"
    assert record.body == "Traceback follows"
    assert (record.state, record.state_reason) == ("CLOSED", "COMPLETED")
    assert (record.author, record.author_type) == ("alice", "User")
    assert record.author_association == "FIRST_TIME_CONTRIBUTOR"
    assert (record.created_at, record.updated_at, record.closed_at) == (
        "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z",
    )
    assert record.labels == ("bug", "provider/aws")
    assert record.assignees == ("maintainer",)
    assert record.participants == ("alice", "maintainer")
    assert (record.milestone, record.reaction_count) == ("v5.1", 4)
    assert (record.comment_count, record.comments_stored) == (2, 2)
    assert record.comments_truncated is False
    assert [(c.comment_id, c.author, c.position, c.comment_type) for c in comments] == [
        ("c1", "maintainer", 0, "comment"),
        ("c2", "alice", 1, "comment"),
    ]


def test_a_pull_request_maps_its_own_fields_and_its_reviews():
    node = pr_node(
        99,
        gql_user("alice"),
        additions=120,
        deletions=4,
        changed_files=6,
        commits=3,
        base_ref="master",
        head_ref="feat/x",
        review_decision="CHANGES_REQUESTED",
        comments=[comment("c1", gql_user("bob"), body="nit")],
        reviews=[
            review("r1", gql_user("maintainer"), state="CHANGES_REQUESTED", body="please split"),
            review("r2", gql_user("carol"), state="APPROVED", body=""),
        ],
    )

    record, comments = thread_from_node("o/r", "prs", node)

    assert (record.kind, record.state, record.merged) == ("prs", "MERGED", True)
    assert (record.merged_by, record.merged_at) == ("maintainer", "2026-01-05T00:00:00Z")
    assert (record.additions, record.deletions, record.changed_files) == (120, 4, 6)
    assert (record.commits, record.base_ref, record.head_ref) == (3, "master", "feat/x")
    assert record.review_decision == "CHANGES_REQUESTED"
    assert record.is_draft is False

    kinds = {(c.comment_id, c.comment_type, c.review_state) for c in comments}
    assert kinds == {
        ("c1", "comment", None),
        ("r1", "review", "CHANGES_REQUESTED"),
        ("r2", "review", "APPROVED"),
    }
    # a review with an empty body is still a verdict, so it is kept
    assert next(c for c in comments if c.comment_id == "r2").body is None
    # comments and reviews are positioned within their own type, not interleaved
    assert [c.position for c in comments if c.comment_type == "review"] == [0, 1]
    # only real comments count towards the cap
    assert record.comments_stored == 1


def test_a_discussion_maps_its_category_answer_and_replies():
    node = discussion_node(
        5,
        gql_user("alice"),
        category="Q&A",
        upvotes=11,
        answer_author=gql_user("carol"),
        comments=[
            comment(
                "c1",
                gql_user("carol"),
                body="set the env var",
                is_answer=True,
                upvotes=4,
                replies=[comment("c1r1", gql_user("alice"), body="that worked, thanks")],
            )
        ],
    )

    record, comments = thread_from_node("o/r", "discussions", node)

    assert (record.kind, record.category, record.upvotes) == ("discussions", "Q&A", 11)
    assert (record.is_answered, record.answer_author) == (True, "carol")
    assert record.answer_at == "2026-01-06T00:00:00Z"

    answer = next(c for c in comments if c.comment_id == "c1")
    reply = next(c for c in comments if c.comment_id == "c1r1")
    assert (answer.is_answer, answer.upvotes, answer.comment_type) == (True, 4, "comment")
    assert (reply.comment_type, reply.parent_id, reply.author) == ("reply", "c1", "alice")


def test_a_capped_thread_is_flagged_not_silently_cut():
    record, comments = thread_from_node(
        "o/r", "issues",
        issue_node(1, gql_user("alice"), comments=[comment("c1", gql_user("bob"))],
                   comments_total=97),
    )
    assert (record.comment_count, record.comments_stored) == (97, 1)
    assert record.comments_truncated is True
    assert len(comments) == 1


def test_a_deleted_author_is_none_and_a_bot_is_relabelled():
    deleted, _ = thread_from_node("o/r", "issues", issue_node(1, None))
    assert (deleted.author, deleted.author_type) == (None, None)

    bot, comments = thread_from_node(
        "o/r", "issues",
        issue_node(2, gql_bot("dependabot"), comments=[comment("c1", gql_bot("renovate"))]),
    )
    assert bot.author_type == "Bot"
    assert comments[0].author_type == "Bot"


# ----------------------------------------------------------------- harvesting


@pytest.mark.parametrize("kind", ["issues", "prs", "discussions"])
def test_every_section_harvests_into_the_same_tables(gql, make_gql_client, store, kind):
    node = {
        "issues": lambda: issue_node(2, gql_user("alice"), comments=[comment("c1", gql_user("bob"))]),
        "prs": lambda: pr_node(2, gql_user("alice"), comments=[comment("c1", gql_user("bob"))]),
        "discussions": lambda: discussion_node(2, gql_user("alice"), comments=[comment("c1", gql_user("bob"))]),
    }[kind]()
    gql.add(OPERATION[kind], thread_body(CONNECTION[kind], [node], total=1))

    result = _harvest(gql, make_gql_client, store, kind)

    assert (result.kind, result.fetched, result.new, result.comments_new) == (kind, 1, 1, 1)
    assert result.complete
    rows = store.thread_rows(kinds=[kind])
    assert {(r["login"], r["role"]) for r in rows} == {("alice", "author"), ("bob", "commenter")}
    # the backfill walks CREATED_AT: stable under a cursor
    assert gql.variables_for(OPERATION[kind])[0]["order"]["field"] == "CREATED_AT"


def test_sections_are_stored_side_by_side(gql, make_gql_client, store):
    gql.add("IssueDetails", thread_body("issues", [issue_node(1, gql_user("alice"))]))
    gql.add("PullRequestDetails", thread_body("pullRequests", [pr_node(1, gql_user("alice"))]))
    gql.add("DiscussionDetails", thread_body("discussions", [discussion_node(1, gql_user("alice"))]))

    for kind in ("issues", "prs", "discussions"):
        _harvest(gql, make_gql_client, store, kind)

    # same number 1 in all three sections: the key is (repo, kind, number)
    assert store.thread_stats()["by_kind"] == {"discussions": 1, "issues": 1, "prs": 1}


def test_a_reviewer_counts_as_involved(gql, make_gql_client, store):
    gql.add(
        "PullRequestDetails",
        thread_body(
            "pullRequests",
            [pr_node(4, gql_user("alice"), reviews=[review("r1", gql_user("carol"))])],
        ),
    )
    _harvest(gql, make_gql_client, store, "prs")

    rows = {r["login"]: r["role"] for r in store.thread_rows()}
    assert rows == {"alice": "author", "carol": "reviewer"}


def test_re_harvesting_reports_no_new_rows(gql, make_gql_client, store):
    gql.add("IssueDetails", thread_body("issues", [issue_node(1, gql_user("alice"))]))
    first = _harvest(gql, make_gql_client, store)
    second = _harvest(gql, make_gql_client, store, fresh=True)

    assert first.new == 1
    assert second.fetched == 1 and second.new == 0


def test_a_finished_backfill_is_skipped_until_fresh(gql, make_gql_client, store):
    gql.add("IssueDetails", thread_body("issues", [issue_node(1, gql_user("alice"))]))
    _harvest(gql, make_gql_client, store)

    again = _harvest(gql, make_gql_client, store)
    assert again.skipped and again.points == 0


def test_backfill_follows_the_cursor(gql, make_gql_client, store):
    gql.add_pages(
        "IssueDetails",
        [
            thread_body("issues", [issue_node(2, gql_user("alice"))], has_next=True, cursor="CUR1", total=2),
            thread_body("issues", [issue_node(1, gql_user("bob"))], total=2),
        ],
    )

    result = _harvest(gql, make_gql_client, store, page_size=1)

    assert (result.pages, result.fetched, result.new) == (2, 2, 2)
    assert gql.variables_for("IssueDetails")[1]["after"] == "CUR1"


def test_max_items_bounds_the_crawl(gql, make_gql_client, store):
    gql.add(
        "IssueDetails",
        thread_body("issues", [issue_node(9, gql_user("alice"))], has_next=True, cursor="C", total=500),
    )
    result = _harvest(gql, make_gql_client, store, page_size=1, max_items=1)

    assert result.pages == 1 and not result.complete


def test_an_unknown_section_is_rejected(gql, make_gql_client, store):
    with pytest.raises(ValueError, match="kind must be one of"):
        _harvest(gql, make_gql_client, store, "stars")


def test_incremental_walks_updated_at_and_stops_at_the_high_water_mark(
    gql, make_gql_client, store
):
    """A two-year-old thread with a comment from today must still come back."""
    old = issue_node(1, gql_user("alice"), created="2024-01-01T00:00:00Z", updated="2026-01-05T00:00:00Z")
    gql.add("IssueDetails", thread_body("issues", [old], total=1))
    _harvest(gql, make_gql_client, store)

    gql.queue.clear()
    gql.add("IssueDetails", thread_body("issues", [old], total=1))
    pass_one = _harvest(gql, make_gql_client, store, incremental=True, unchanged_streak=1)

    assert pass_one.stopped_early and pass_one.fetched == 0
    assert gql.variables_for("IssueDetails")[-1]["order"]["field"] == "UPDATED_AT"
    assert store.get_crawl_state("threads:o/r:issues:updated")["high_water"] == "2026-01-05T00:00:00Z"

    touched = issue_node(
        2, gql_user("bob"), created="2026-02-01T00:00:00Z", updated="2026-02-10T00:00:00Z",
        comments=[comment("c9", gql_user("carol"))],
    )
    gql.queue.clear()
    gql.add("IssueDetails", thread_body("issues", [touched, old], total=2))
    pass_two = _harvest(gql, make_gql_client, store, incremental=True)

    assert pass_two.stopped_early
    assert (pass_two.fetched, pass_two.new, pass_two.comments_new) == (1, 1, 1)
    assert store.get_crawl_state("threads:o/r:issues:updated")["high_water"] == "2026-02-10T00:00:00Z"


def test_each_section_keeps_its_own_crawl_state(gql, make_gql_client, store):
    gql.add("IssueDetails", thread_body("issues", [issue_node(1, gql_user("alice"))]))
    _harvest(gql, make_gql_client, store, "issues")

    assert store.get_crawl_state("threads:o/r:issues") is not None
    assert store.get_crawl_state("threads:o/r:prs") is None


# ------------------------------------------------------------- filling capped threads


def test_fill_pages_a_capped_thread_start_to_finish(gql, make_gql_client, store):
    """The old "tails" approach fetched only the newest slice, leaving a hole in the
    middle. Fill walks the whole connection and clears the flag."""
    node = issue_node(1, gql_user("alice"), comments=[comment("c1", gql_user("bob"))],
                      comments_total=5)
    gql.add(OPERATION["issues"], thread_body(CONNECTION["issues"], [node]))
    result = _harvest(gql, make_gql_client, store, "issues")
    assert result.truncated == 1

    gql.add_pages(
        "Fill",
        [
            {"repository": {"issue": {"number": 1, "comments": connection(
                nodes=[comment(f"c{i}", gql_user("bob")) for i in (1, 2, 3)],
                total=5, has_next=True, cursor="C1")}}},
            {"repository": {"issue": {"number": 1, "comments": connection(
                nodes=[comment("c4", gql_user("carol")), comment("c5", gql_user("alice"))],
                total=5)}}},
        ],
    )

    async def go():
        async with make_gql_client(gql) as client:
            return await fill_capped_threads(client, store, "o/r", "issues")

    done, added, _points = run(go())

    assert done == 1 and added == 4  # c1 already stored; idempotent overlap is free
    rows = store.thread_comment_rows(kinds=["issues"])
    assert [(r["comment_id"], r["position"]) for r in rows] == [
        ("c1", 0), ("c2", 1), ("c3", 2), ("c4", 3), ("c5", 4)
    ]
    assert store.threads_needing_tail() == []  # flag cleared: nothing missing
    # the follow-up page resumed from the cursor
    assert gql.variables_for("Fill")[-1]["after"] == "C1"


def test_fill_also_collects_replies_inside_the_pages(gql, make_gql_client, store):
    node = discussion_node(2, gql_user("alice"), comments=[comment("d1", gql_user("bob"))],
                           comments_total=2)
    gql.add(OPERATION["discussions"], thread_body(CONNECTION["discussions"], [node]))
    _harvest(gql, make_gql_client, store, "discussions")

    gql.add("Fill", {"repository": {"discussion": {"number": 2, "comments": connection(
        nodes=[comment("d1", gql_user("bob")),
               comment("d2", gql_user("carol"),
                       replies=[comment("d2r1", gql_user("alice"), body="thanks!")])],
        total=2)}}})

    async def go():
        async with make_gql_client(gql) as client:
            return await fill_capped_threads(client, store, "o/r", "discussions")

    run(go())
    rows = store.thread_comment_rows(kinds=["discussions"])
    kinds = {(r["comment_id"], r["comment_type"]) for r in rows}
    assert ("d2r1", "reply") in kinds and ("d2", "comment") in kinds


# ------------------------------------------------------------------ involvement


@pytest.fixture
def populated(store):
    """alice filed an issue and a discussion; bob commented and reviewed; a bot filed."""
    for login in ("alice", "bob"):
        store.upsert_profile(Profile(login=login, type="User", name=login.title(), company="Corp"))
    store.add_interactions([Interaction("o/r", "alice", "issues", "User", "2026-01-01T00:00:00Z")])
    store.add_emails("alice", [EmailCandidate("alice@corp.io", "gql_profile", 0.95)])
    store.upsert_threads(
        [
            ThreadRecord("o/r", "issues", 7, title="Crash", body="line one\nline two",
                         author="alice", author_type="User", created_at="2026-01-01T00:00:00Z",
                         labels=("bug",), comment_count=2, comments_stored=2),
            ThreadRecord("o/r", "prs", 8, title="Fix crash", author="alice",
                         author_type="User", created_at="2026-01-03T00:00:00Z",
                         state="MERGED", merged=True, additions=10, deletions=2),
            ThreadRecord("o/r", "discussions", 9, title="How do I?", author="alice",
                         author_type="User", created_at="2026-01-04T00:00:00Z",
                         category="Q&A", is_answered=True, answer_author="bob"),
            ThreadRecord("o/r", "issues", 10, title="Bump deps", author="dependabot[bot]",
                         author_type="Bot", created_at="2026-01-02T00:00:00Z"),
        ]
    )
    store.add_thread_comments(
        [
            ThreadComment("o/r", "issues", 7, "c1", author="bob", author_type="User",
                          body="me too", created_at="2026-01-02T00:00:00Z", position=0),
            ThreadComment("o/r", "issues", 7, "c2", author="alice", author_type="User",
                          body="repro", created_at="2026-01-03T00:00:00Z", position=1),
            ThreadComment("o/r", "prs", 8, "r1", comment_type="review", author="bob",
                          author_type="User", body="lgtm", review_state="APPROVED",
                          created_at="2026-01-04T00:00:00Z", position=0),
            ThreadComment("o/r", "discussions", 9, "d1", author="bob", author_type="User",
                          body="use the env var", is_answer=True, upvotes=3,
                          created_at="2026-01-05T00:00:00Z", position=0),
        ]
    )
    return store


def test_involvement_covers_authors_commenters_and_reviewers(populated):
    rows = populated.thread_rows()

    assert {(r["login"], r["kind"], r["role"]) for r in rows} == {
        ("alice", "issues", "author"),
        ("alice", "prs", "author"),
        ("alice", "discussions", "author"),
        ("bob", "issues", "commenter"),
        ("bob", "prs", "reviewer"),
        ("bob", "discussions", "commenter"),
    }
    # alice authored #7 and also commented on it; authoring is the stronger signal
    issue = next(r for r in rows if r["login"] == "alice" and r["kind"] == "issues")
    assert (issue["role"], issue["own_comments"]) == ("author", 1)


def test_the_answerer_is_flagged(populated):
    bob = next(
        r for r in populated.thread_rows(kinds=["discussions"]) if r["login"] == "bob"
    )
    assert bob["wrote_the_answer"] is True
    assert bob["is_answered"] == 1 and bob["answer_author"] == "bob"


def test_each_row_carries_the_whole_thread(populated):
    row = next(r for r in populated.thread_rows(kinds=["issues"]) if r["login"] == "bob")

    assert [(c["author"], c["body"]) for c in row["comments"]] == [
        ("bob", "me too"),
        ("alice", "repro"),
    ]
    assert row["own_comment_bodies"] == ["me too"]


def test_filters(populated):
    assert {r["login"] for r in populated.thread_rows(role="reviewer")} == {"bob"}
    assert {r["kind"] for r in populated.thread_rows(kinds=["prs"])} == {"prs"}
    assert {r["login"] for r in populated.thread_rows(only_with_email=True)} == {"alice"}
    assert {r["login"] for r in populated.thread_rows(logins=["bob"])} == {"bob"}
    assert "dependabot[bot]" not in {r["login"] for r in populated.thread_rows()}

    populated.suppress(["bob"])
    assert "bob" not in {r["login"] for r in populated.thread_rows()}


def test_an_unknown_role_or_section_is_rejected(populated):
    with pytest.raises(ValueError, match="role must be one of"):
        populated.thread_rows(role="lurker")
    with pytest.raises(ValueError, match="kind must be one of"):
        populated.thread_rows(kinds=["stars"])


# ---------------------------------------------------------------------- export


def test_the_csv_carries_the_conversation(populated, tmp_path):
    path = tmp_path / "threads.csv"
    count, fmt = write_threads(populated, path)
    assert (count, fmt) == (6, "csv")

    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].split(",") == THREAD_COLUMNS
    # every comment, with its type, author and timestamp, inside the row
    assert "[comment] bob @ 2026-01-02T00:00:00Z: me too" in text
    assert "[review APPROVED] bob @ 2026-01-04T00:00:00Z: lgtm" in text
    assert "[comment ANSWER] bob @ 2026-01-05T00:00:00Z: use the env var" in text
    # bodies are flattened so one thread stays one row
    assert "line one line two" in text


def test_jsonl_keeps_bodies_and_nests_the_comments(populated, tmp_path):
    path = tmp_path / "threads.jsonl"
    write_threads(populated, path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    issue = next(r for r in records if r["section"] == "issues" and r["login"] == "bob")
    assert issue["body"] == "line one\nline two"  # verbatim: JSONL is not a spreadsheet
    assert [c["author"] for c in issue["comments"]] == ["bob", "alice"]
    assert issue["own_comments_text"] == ["me too"]


def test_body_and_comment_truncation_apply_only_to_the_long_columns(populated, tmp_path):
    path = tmp_path / "threads.csv"
    write_threads(populated, path, body_chars=6, comment_chars=4)
    text = path.read_text(encoding="utf-8")

    assert "line o" not in text  # 6 chars means 5 + an ellipsis
    assert "Crash" in text  # titles are untouched


def test_the_comment_export_is_in_thread_order(populated, tmp_path):
    path = tmp_path / "comments.csv"
    count, _ = write_thread_comments(populated, path)
    lines = path.read_text(encoding="utf-8").splitlines()

    assert count == 4
    assert lines[0].split(",") == COMMENT_COLUMNS
    sections = [line.split(",")[1] for line in lines[1:]]
    assert sections == ["discussions", "issues", "issues", "prs"]


def test_the_lead_export_summarises_every_section(populated, tmp_path):
    path = tmp_path / "leads.csv"
    write_export(populated, path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == COLUMNS

    alice = dict(zip(COLUMNS, lines[1].split(",")))
    assert alice["login"] == "alice"
    assert (alice["issues_authored"], alice["issues_commented"]) == ("1", "0")
    assert (alice["prs_authored"], alice["prs_reviewed"]) == ("1", "0")
    assert alice["discussions_authored"] == "1"
    assert alice["last_pr_at"] == "2026-01-03T00:00:00Z"


def test_summary_columns_are_zero_before_the_thread_stage_has_run(store, tmp_path):
    """The new columns must not break an install that never ran `gitscout threads`."""
    store.upsert_profile(Profile(login="alice", type="User"))
    store.add_interactions([Interaction("o/r", "alice", "forks", "User", "2026-01-01T00:00:00Z")])
    path = tmp_path / "leads.csv"

    write_export(store, path)
    row = dict(zip(COLUMNS, path.read_text(encoding="utf-8").splitlines()[1].split(",")))
    assert row["issues_authored"] == "0" and row["prs_reviewed"] == "0"
    assert row["last_discussion_at"] == ""


#: Excel refuses to display more than this in one cell, and Python's csv reader
#: defaults to a 128 KB field limit. A CSV that exceeds either is not openable.
EXCEL_CELL_LIMIT = 32767


def test_no_csv_cell_can_exceed_what_a_spreadsheet_opens(store, tmp_path):
    """Regression: `comments_json` ignored the caps and produced a 1.4 MB cell."""
    store.upsert_profile(Profile(login="alice", type="User"))
    store.add_interactions([Interaction("o/r", "alice", "issues", "User", "2026-01-01T00:00:00Z")])
    store.upsert_threads(
        [ThreadRecord("o/r", "issues", 1, title="Huge", body="x" * 50_000,
                      author="alice", author_type="User",
                      created_at="2026-01-01T00:00:00Z", comment_count=40)]
    )
    store.add_thread_comments(
        [
            ThreadComment("o/r", "issues", 1, f"c{i}", author="bob", author_type="User",
                          body="y" * 20_000, created_at="2026-01-02T00:00:00Z", position=i)
            for i in range(40)
        ]
    )

    path = tmp_path / "threads.csv"
    write_threads(store, path, body_chars=400, comment_chars=250)

    import csv as _csv

    _csv.field_size_limit(10_000_000)
    rows = list(_csv.DictReader(path.open(encoding="utf-8")))
    widest = max(max(len(v or "") for v in r.values()) for r in rows)
    assert widest <= EXCEL_CELL_LIMIT, f"widest cell {widest} would not open in Excel"


def test_jsonl_is_where_the_untruncated_text_lives(store, tmp_path):
    """The caps are a spreadsheet concession; JSONL must still carry everything."""
    store.upsert_profile(Profile(login="alice", type="User"))
    store.add_interactions([Interaction("o/r", "alice", "issues", "User", "2026-01-01T00:00:00Z")])
    store.upsert_threads(
        [ThreadRecord("o/r", "issues", 1, title="Huge", body="x" * 50_000,
                      author="alice", author_type="User", created_at="2026-01-01T00:00:00Z")]
    )
    store.add_thread_comments(
        [ThreadComment("o/r", "issues", 1, "c1", author="bob", author_type="User",
                       body="y" * 20_000, created_at="2026-01-02T00:00:00Z")]
    )

    path = tmp_path / "threads.jsonl"
    write_threads(store, path, body_chars=400, comment_chars=250)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    assert len(record["body"]) == 50_000
    assert len(record["comments"][0]["body"]) == 20_000


# ---------------------------------------------------------------- profile extras

from gitscout.threads_gql import normalize_linkedin, resolve_social  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://LinkedIn.com/in/Jane-Doe/?trk=x", "https://www.linkedin.com/in/jane-doe"),
        ("https://www.linkedin.com/in/abc123", "https://www.linkedin.com/in/abc123"),
        ("linkedin.com/pub/old-style/1/2/3", "https://www.linkedin.com/in/old-style"),
        ("https://www.linkedin.com/company/acme", None),  # a company is not a person
        ("https://jane.dev", None),
        (None, None),
    ],
)
def test_linkedin_urls_are_canonicalised_or_rejected(raw, expected):
    assert normalize_linkedin(raw) == expected


def test_social_lookup_takes_only_what_the_person_published(gql, make_gql_client, store):
    store.upsert_profile(Profile(login="alice", type="User", blog="https://jane.dev"))
    store.upsert_profile(Profile(login="bob", type="User", blog="linkedin.com/in/Bob-B"))
    store.upsert_profile(Profile(login="carol", type="User"))
    gql.add(
        "Social",
        {
            "u0": {"login": "alice", "repositories": {"totalCount": 12},
                   "socialAccounts": {"nodes": [
                       {"provider": "TWITTER", "url": "https://x.com/alice"},
                       {"provider": "LINKEDIN", "url": "https://LinkedIn.com/in/Alice-A/"}]}},
            "u1": {"login": "bob", "repositories": {"totalCount": 3},
                   "socialAccounts": {"nodes": []}},
            "u2": {"login": "carol", "repositories": {"totalCount": 0},
                   "socialAccounts": {"nodes": []}},
        },
    )

    async def go():
        async with make_gql_client(gql) as client:
            return await resolve_social(client, store)

    looked, found = run(go())

    assert (looked, found) == (3, 2)
    rows = {r["login"]: r for r in store._conn.execute(
        "SELECT login, linkedin_url, public_repos, social_fetched_at FROM users")}
    assert rows["alice"]["linkedin_url"] == "https://www.linkedin.com/in/alice-a"
    assert rows["bob"]["linkedin_url"] == "https://www.linkedin.com/in/bob-b"  # from website field
    assert rows["carol"]["linkedin_url"] is None  # nothing published, nothing invented
    assert rows["alice"]["public_repos"] == 12
    assert all(r["social_fetched_at"] for r in rows.values())
    # a second run skips people already looked up
    assert store.logins_needing_social() == []


# -------------------------------------------- full content: urls, code comments

from gqlhelpers import review_comment  # noqa: E402


def test_every_comment_carries_its_github_url():
    _record, comments = thread_from_node(
        "o/r", "issues", issue_node(1, gql_user("a"), comments=[comment("c1", gql_user("b"))])
    )
    assert comments[0].url == "https://github.com/o/r/comments/c1"


def test_inline_code_review_comments_are_collected_with_file_and_review():
    node = pr_node(
        9, gql_user("alice"),
        reviews=[review("r1", gql_user("m"), state="CHANGES_REQUESTED")],
        review_thread_comments=[
            review_comment("rc1", gql_user("m"), review_id="r1", path="pkg/db.go"),
            review_comment("rc2", gql_user("alice"), review_id="r1", path="pkg/db.go",
                           body="done, renamed"),
        ],
    )
    _record, comments = thread_from_node("o/r", "prs", node)
    inline = [c for c in comments if c.comment_type == "review_comment"]

    assert [(c.comment_id, c.parent_id, c.file_path) for c in inline] == [
        ("rc1", "r1", "pkg/db.go"), ("rc2", "r1", "pkg/db.go"),
    ]
    assert inline[0].url.endswith("#discussion_rc1")
    # inline comments never count against the thread-comments cap
    assert _record.comments_stored == 0


def test_review_overflow_is_completed_inline_during_harvest(gql, make_gql_client, store):
    """76 PRs sat at the 20-review cap with no flag. Now the harvest notices
    `hasNextPage` and drains the rest before moving on."""
    node = pr_node(4, gql_user("alice"),
                   reviews=[review("r1", gql_user("m"))],
                   reviews_has_next=True, reviews_cursor="RC1")
    gql.add(OPERATION["prs"], thread_body(CONNECTION["prs"], [node]))
    gql.add("ReviewsFill", {"repository": {"pullRequest": {"number": 4, "reviews": connection(
        nodes=[review("r2", gql_user("carol"), state="APPROVED")], total=2)}}})

    result = _harvest(gql, make_gql_client, store, "prs")

    assert gql.variables_for("ReviewsFill")[0]["after"] == "RC1"
    rows = {r["comment_id"]: r for r in store.thread_comment_rows(kinds=["prs"])}
    assert {"r1", "r2"} <= set(rows)
    assert rows["r2"]["review_state"] == "APPROVED"
    assert result.comments_new == 2


def test_reply_overflow_is_completed_inline_during_harvest(gql, make_gql_client, store):
    """313 discussion comments sat at the old 5-reply cap with no flag."""
    parent = comment("d1", gql_user("bob"),
                     replies=[comment("d1r1", gql_user("alice"))])
    parent["replies"]["pageInfo"] = {"hasNextPage": True, "endCursor": "RP1"}
    node = discussion_node(7, gql_user("alice"), comments=[parent])
    gql.add(OPERATION["discussions"], thread_body(CONNECTION["discussions"], [node]))
    gql.add("RepliesFill", {"node": {"id": "d1", "replies": connection(
        nodes=[comment("d1r2", gql_user("carol"), body="late reply")], total=2)}})

    result = _harvest(gql, make_gql_client, store, "discussions")

    assert gql.variables_for("RepliesFill")[0] == {"id": "d1", "first": 50, "after": "RP1"}
    rows = {r["comment_id"]: r for r in store.thread_comment_rows(kinds=["discussions"])}
    assert rows["d1r2"]["comment_type"] == "reply" and rows["d1r2"]["body"] == "late reply"
    assert result.comments_new == 3  # d1 + both replies
