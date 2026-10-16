"""SQLite persistence layer (stdlib only).

Layers:  interactions (raw) -> users (profiles) -> emails (discovered, scored)
All writes are idempotent so any stage can be re-run safely.

Two tables exist purely for the scheduled path:

* ``crawl_state`` -- a cursor (for resuming a backfill) *and* a high-water mark (the
  newest interaction already seen) per repo+kind. The high-water mark is what lets a
  cron run read only page 1 and stop.
* ``runs`` -- an audit log, so you can see what each scheduled run actually found.

``checkpoints`` is the REST path's older per-page checkpoint and is left untouched.

``threads`` / ``thread_comments`` sit beside that stack rather than inside it.
``interactions`` is keyed (repo, login, kind), i.e. one row per person per repo, so
it can record *that* someone filed issues but never *which* ones. The thread tables
are keyed by the thread itself -- an issue, a pull request or a discussion -- and the
``thread_involvement`` view joins every direction of involvement (opened it,
commented on it, reviewed it) back to a login.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    NON_HUMAN_TYPES,
    THREAD_KINDS,
    THREAD_ROLES,
    EmailCandidate,
    Interaction,
    Profile,
    RunRecord,
    ThreadComment,
    ThreadRecord,
)


def _sql_identifier_list(values: Iterable[str]) -> str:
    """Render a fixed set of known-safe literals for an IN clause.

    Only used for ``NON_HUMAN_TYPES``, which is a module constant, and every value is
    asserted alphanumeric so this can never carry caller input into SQL.
    """
    out = []
    for value in sorted(values):
        if not value.isalnum():
            raise ValueError(f"refusing to inline non-alphanumeric SQL literal: {value!r}")
        out.append(f"'{value}'")
    return ",".join(out)


#: Account types that are never a person to contact: bots, orgs, mannequins.
_NON_HUMAN_SQL = _sql_identifier_list(NON_HUMAN_TYPES)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS interactions (
    repo        TEXT NOT NULL,
    login       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    user_type   TEXT,
    occurred_at TEXT,
    extra       TEXT,
    PRIMARY KEY (repo, login, kind)
);
CREATE INDEX IF NOT EXISTS idx_interactions_login ON interactions(login);

CREATE TABLE IF NOT EXISTS users (
    login         TEXT PRIMARY KEY,
    type          TEXT,
    name          TEXT,
    company       TEXT,
    bio           TEXT,
    location      TEXT,
    blog          TEXT,
    twitter       TEXT,
    public_email  TEXT,
    hireable      INTEGER,
    followers     INTEGER,
    profile_found INTEGER NOT NULL DEFAULT 1,
    fetched_at    TEXT,
    discovered_at TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    login      TEXT NOT NULL,
    email      TEXT NOT NULL,
    source     TEXT NOT NULL,
    confidence REAL NOT NULL,
    found_at   TEXT NOT NULL,
    PRIMARY KEY (login, email, source)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    key        TEXT PRIMARY KEY,
    next_url   TEXT,
    done       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS suppression (
    value    TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_state (
    key         TEXT PRIMARY KEY,
    cursor      TEXT,
    done        INTEGER NOT NULL DEFAULT 0,
    high_water  TEXT,
    total_count INTEGER,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    repo               TEXT NOT NULL,
    kind               TEXT NOT NULL,          -- issues | prs | discussions
    number             INTEGER NOT NULL,
    url                TEXT,
    title              TEXT,
    body               TEXT,
    state              TEXT,
    state_reason       TEXT,
    author             TEXT,
    author_type        TEXT,
    author_association TEXT,
    created_at         TEXT,
    updated_at         TEXT,
    closed_at          TEXT,
    locked             INTEGER,
    comment_count      INTEGER,
    reaction_count     INTEGER,
    labels             TEXT,                   -- JSON array
    assignees          TEXT,                   -- JSON array
    milestone          TEXT,
    participants       TEXT,                   -- JSON array
    node_id            TEXT,
    comments_stored    INTEGER NOT NULL DEFAULT 0,
    comments_truncated INTEGER NOT NULL DEFAULT 0,
    -- pull requests only
    merged             INTEGER,
    merged_at          TEXT,
    merged_by          TEXT,
    is_draft           INTEGER,
    additions          INTEGER,
    deletions          INTEGER,
    changed_files      INTEGER,
    commits            INTEGER,
    base_ref           TEXT,
    head_ref           TEXT,
    review_decision    TEXT,
    -- discussions only
    category           TEXT,
    category_emoji     TEXT,
    is_answerable      INTEGER,
    closed             INTEGER,
    upvotes            INTEGER,
    is_answered        INTEGER,
    answer_author      TEXT,
    answer_at          TEXT,
    added_at           TEXT,
    fetched_at         TEXT,
    PRIMARY KEY (repo, kind, number)
);

CREATE TABLE IF NOT EXISTS thread_comments (
    repo               TEXT NOT NULL,
    kind               TEXT NOT NULL,
    number             INTEGER NOT NULL,
    comment_id         TEXT NOT NULL,
    comment_type       TEXT NOT NULL DEFAULT 'comment',  -- comment | review | reply
    author             TEXT,
    author_type        TEXT,
    author_association TEXT,
    body               TEXT,
    created_at         TEXT,
    updated_at         TEXT,
    position           INTEGER NOT NULL DEFAULT 0,
    is_answer          INTEGER,
    upvotes            INTEGER,
    review_state       TEXT,
    parent_id          TEXT,
    added_at           TEXT,
    PRIMARY KEY (repo, kind, number, comment_id)
);

-- "Involved in a thread" = opened it, commented on it, or reviewed it. One row per
-- person per thread: MIN() over the role names resolves someone who did several
-- things to the strongest one, and the names are alphabetical on purpose
-- ('author' < 'commenter' < 'reviewer') so no CASE expression is needed.
CREATE VIEW IF NOT EXISTS thread_involvement AS
SELECT repo, kind, number, login, author_type, MIN(role) AS role
FROM (
    SELECT repo, kind, number, author AS login, author_type, 'author' AS role
    FROM threads WHERE author IS NOT NULL
    UNION ALL
    SELECT repo, kind, number, author AS login, author_type,
           CASE WHEN comment_type = 'review' THEN 'reviewer' ELSE 'commenter' END
    FROM thread_comments WHERE author IS NOT NULL
)
GROUP BY repo, kind, number, login;

CREATE TABLE IF NOT EXISTS repositories (
    repo            TEXT PRIMARY KEY,      -- owner/name, the key every record uses
    repository_id   INTEGER,               -- GitHub databaseId: survives renames
    node_id         TEXT,
    owner           TEXT,
    name            TEXT,
    url             TEXT,
    description     TEXT,
    homepage        TEXT,
    stars           INTEGER,
    forks           INTEGER,
    language        TEXT,
    topics          TEXT,                  -- JSON array
    is_archived     INTEGER,
    fetched_at      TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mode             TEXT NOT NULL,
    targets          TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    interactions_new INTEGER NOT NULL DEFAULT 0,
    users_new        INTEGER NOT NULL DEFAULT 0,
    emails_new       INTEGER NOT NULL DEFAULT 0,
    points_used      INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'running',
    error            TEXT,
    extra            TEXT
);
"""

#: Columns added after v0.1. Applied to existing databases on open.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("interactions", "added_at", "TEXT"),
    ("users", "node_id", "TEXT"),
    ("users", "account_created_at", "TEXT"),
    ("users", "score", "REAL"),
    ("users", "commit_probed_at", "TEXT"),
    ("users", "provider_probed_at", "TEXT"),
    ("thread_comments", "changed_at", "TEXT"),
    ("threads", "category_emoji", "TEXT"),
    ("threads", "is_answerable", "INTEGER"),
    ("threads", "closed", "INTEGER"),
    ("thread_comments", "url", "TEXT"),
    ("thread_comments", "file_path", "TEXT"),
    ("threads", "source_deleted_at", "TEXT"),  # set by the deletion sweep; never deleted
    ("users", "linkedin_url", "TEXT"),
    ("users", "public_repos", "INTEGER"),
    ("users", "social_fetched_at", "TEXT"),
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_interactions_added ON interactions(added_at)",
    "CREATE INDEX IF NOT EXISTS idx_emails_login ON emails(login)",
    "CREATE INDEX IF NOT EXISTS idx_users_score ON users(score)",
    "CREATE INDEX IF NOT EXISTS idx_threads_author ON threads(author)",
    "CREATE INDEX IF NOT EXISTS idx_threads_created ON threads(kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_thread_comments_author ON thread_comments(author)",
    "CREATE INDEX IF NOT EXISTS idx_thread_comments_thread "
    "ON thread_comments(repo, kind, number)",
    "CREATE INDEX IF NOT EXISTS idx_thread_comments_changed ON thread_comments(changed_at)",
    "CREATE INDEX IF NOT EXISTS idx_users_fetched ON users(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_threads_fetched ON threads(fetched_at)",
)


def _now() -> str:
    """Microseconds, not seconds: `--new-only` compares a run's start against
    `interactions.added_at`, and second resolution made two runs in the same second
    indistinguishable."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


#: The local hour the circular mean of someone's activity is assumed to sit on.
#: Midday, not mid-afternoon: anchoring later pushed every estimate 2-3 hours east.
LOCAL_MIDDAY = 12


def estimate_utc_offset(hours: Mapping[str, int] | None) -> int | None:
    """Guess someone's UTC offset from when they are active. An estimate, not a fact.

    GitHub exposes no timezone and normalises every timestamp to UTC, so the only
    signal left is *when* a person posts. People cluster their activity in a working
    day, so the circular mean of their posting hours lands near local midday, and the
    gap between that and UTC is the offset.

    Measured against 173 GitHub users whose profile location names an unambiguous
    country: median error 2 hours, 71% within 3 hours. Someone who posts mostly in the
    evening reads as further west than they are, and a maintainer who answers issues
    across the clock reads as mid-Atlantic no matter where they live. Returns None
    below a handful of events, where the guess would be pure noise.
    """
    if not hours:
        return None
    counts = {int(h): int(n) for h, n in hours.items()}
    total = sum(counts.values())
    if total < 5:
        return None

    # Circular mean: hours wrap, so 23:00 and 01:00 are two apart, not twenty-two.
    import math

    x = sum(n * math.cos(2 * math.pi * h / 24) for h, n in counts.items())
    y = sum(n * math.sin(2 * math.pi * h / 24) for h, n in counts.items())
    if x == 0 and y == 0:
        return None
    mean_hour = (math.degrees(math.atan2(y, x)) / 15) % 24

    offset = round(LOCAL_MIDDAY - mean_hour)
    # Fold into the real range of inhabited offsets.
    if offset > 12:
        offset -= 24
    elif offset < -11:
        offset += 24
    return offset


class Store:
    def __init__(self, path: str | Path = "gitscout.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add post-v0.1 columns to databases created by an older version."""
        with self._conn:
            for table, column, decl in MIGRATIONS:
                cols = {
                    r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
                }
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            for stmt in INDEXES:
                self._conn.execute(stmt)
            # `closed` was added after issues and PRs had already been harvested. It
            # is derivable from `state`, so rows collected before the column existed
            # are corrected here rather than refetched -- a re-crawl of a large repo
            # costs hundreds of GraphQL points to learn something already stored.
            self._conn.execute(
                "UPDATE threads SET closed = (state IN ('CLOSED', 'MERGED')) "
                "WHERE closed IS NULL AND state IS NOT NULL"
            )
        self._migrate_issue_tables()

    def _migrate_issue_tables(self) -> None:
        """Fold the first-cut `issues`/`issue_comments` tables into `threads`.

        The first version of the thread stage only handled issues and gave them their
        own tables. Harvesting costs GraphQL points, so the rows are copied across
        rather than refetched, and the old tables are dropped once they are empty of
        anything new.
        """
        existing = {
            r["name"]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('issues', 'issue_comments', 'issue_involvement')"
            )
        }
        if not existing:
            return
        with self._conn:
            if "issues" in existing:
                self._conn.execute(
                    "INSERT OR IGNORE INTO threads "
                    "(repo, kind, number, url, title, body, state, state_reason, author, "
                    " author_type, author_association, created_at, updated_at, closed_at, "
                    " locked, comment_count, reaction_count, labels, assignees, milestone, "
                    " participants, node_id, comments_stored, comments_truncated, "
                    " added_at, fetched_at) "
                    "SELECT repo, 'issues', number, url, title, body, state, state_reason, "
                    " author, author_type, author_association, created_at, updated_at, "
                    " closed_at, locked, comment_count, reaction_count, labels, assignees, "
                    " milestone, participants, node_id, comments_stored, comments_truncated, "
                    " added_at, fetched_at FROM issues"
                )
            if "issue_comments" in existing:
                self._conn.execute(
                    "INSERT OR IGNORE INTO thread_comments "
                    "(repo, kind, number, comment_id, comment_type, author, author_type, "
                    " author_association, body, created_at, updated_at, position, added_at) "
                    "SELECT repo, 'issues', number, comment_id, 'comment', author, "
                    " author_type, author_association, body, created_at, updated_at, "
                    " position, added_at FROM issue_comments"
                )
            if "issue_involvement" in existing:
                self._conn.execute("DROP VIEW IF EXISTS issue_involvement")
            self._conn.execute("DROP TABLE IF EXISTS issue_comments")
            self._conn.execute("DROP TABLE IF EXISTS issues")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- interactions

    def add_interactions(self, rows: Iterable[Interaction]) -> int:
        """Insert new interactions, ignoring duplicates. Returns the number of NEW rows."""
        stamp = _now()
        data = [
            (r.repo, r.login, r.kind, r.user_type, r.occurred_at, r.extra, stamp) for r in rows
        ]
        if not data:
            return 0
        with self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO interactions "
                "(repo, login, kind, user_type, occurred_at, extra, added_at) "
                "VALUES (?,?,?,?,?,?,?)",
                data,
            )
            return self._conn.total_changes - before

    # ------------------------------------------------------------ checkpoints

    def get_checkpoint(self, key: str) -> tuple[str | None, bool] | None:
        row = self._conn.execute(
            "SELECT next_url, done FROM checkpoints WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else (row["next_url"], bool(row["done"]))

    def set_checkpoint(self, key: str, next_url: str | None, done: bool) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO checkpoints (key, next_url, done, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET next_url=excluded.next_url, "
                "done=excluded.done, updated_at=excluded.updated_at",
                (key, next_url, int(done), _now()),
            )

    def clear_checkpoint(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM checkpoints WHERE key = ?", (key,))

    # ----------------------------------------------------------- crawl state

    def get_crawl_state(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT cursor, done, high_water, total_count FROM crawl_state WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return {
            "cursor": row["cursor"],
            "done": bool(row["done"]),
            "high_water": row["high_water"],
            "total_count": row["total_count"],
        }

    def set_crawl_state(
        self,
        key: str,
        *,
        cursor: str | None = None,
        done: bool | None = None,
        high_water: str | None = None,
        total_count: int | None = None,
    ) -> None:
        """Patch a crawl state. Only the fields you pass are written.

        ``high_water`` only ever moves forward, so an interrupted incremental run can
        never make a later run skip interactions it has not actually recorded.
        """
        current = self.get_crawl_state(key) or {
            "cursor": None,
            "done": False,
            "high_water": None,
            "total_count": None,
        }
        new_hw = current["high_water"]
        if high_water and (new_hw is None or high_water > new_hw):
            new_hw = high_water
        with self._conn:
            self._conn.execute(
                "INSERT INTO crawl_state (key, cursor, done, high_water, total_count, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "cursor=excluded.cursor, done=excluded.done, high_water=excluded.high_water, "
                "total_count=excluded.total_count, updated_at=excluded.updated_at",
                (
                    key,
                    cursor if cursor is not None else current["cursor"],
                    int(current["done"] if done is None else done),
                    new_hw,
                    total_count if total_count is not None else current["total_count"],
                    _now(),
                ),
            )

    # ------------------------------------------------------- deletion sweep
    #
    # GitHub never tells an incremental crawl that an issue was deleted or transferred;
    # it simply stops listing it. The sweep compares the numbers that still exist with
    # the numbers stored and marks the difference, instead of deleting anything: the
    # row, its comments and its history stay, the outreach dataset leaves it out.

    def thread_numbers(self, repo: str, kind: str, *, include_deleted: bool = False) -> set[int]:
        sql = "SELECT number FROM threads WHERE repo = ? AND kind = ?"
        if not include_deleted:
            sql += " AND source_deleted_at IS NULL"
        return {int(r["number"]) for r in self._conn.execute(sql, (repo, kind))}

    def deleted_thread_numbers(self, repo: str, kind: str) -> set[int]:
        return {
            int(r["number"]) for r in self._conn.execute(
                "SELECT number FROM threads WHERE repo = ? AND kind = ? AND source_deleted_at IS NOT NULL",
                (repo, kind),
            )
        }

    def mark_threads_deleted(self, repo: str, kind: str, numbers: Iterable[int], at: str | None = None) -> int:
        """Record that these threads no longer exist on GitHub. `fetched_at` moves so
        the next push carries the change. Returns how many rows changed."""
        nums = sorted({int(n) for n in numbers})
        if not nums:
            return 0
        now = at or _now()
        changed = 0
        with self._conn:
            for start in range(0, len(nums), 500):
                chunk = nums[start:start + 500]
                marks = ",".join("?" * len(chunk))
                cur = self._conn.execute(
                    f"UPDATE threads SET source_deleted_at = ?, fetched_at = ? WHERE repo = ? AND kind = ? "
                    f"AND source_deleted_at IS NULL AND number IN ({marks})",
                    (now, now, repo, kind, *chunk),
                )
                changed += cur.rowcount
        return changed

    def restore_threads(self, repo: str, kind: str, numbers: Iterable[int], at: str | None = None) -> int:
        """A thread marked deleted that GitHub lists again (restored, transferred back)."""
        nums = sorted({int(n) for n in numbers})
        if not nums:
            return 0
        now = at or _now()
        changed = 0
        with self._conn:
            for start in range(0, len(nums), 500):
                chunk = nums[start:start + 500]
                marks = ",".join("?" * len(chunk))
                cur = self._conn.execute(
                    f"UPDATE threads SET source_deleted_at = NULL, fetched_at = ? WHERE repo = ? AND kind = ? "
                    f"AND source_deleted_at IS NOT NULL AND number IN ({marks})",
                    (now, repo, kind, *chunk),
                )
                changed += cur.rowcount
        return changed

    def crawl_states(self, contains: str) -> dict[str, dict[str, Any]]:
        """Every crawl state whose key mentions `contains` (a repository): the
        checkpoint a worker copies to Postgres after a run and restores on a new disk."""
        rows = self._conn.execute(
            "SELECT key, cursor, done, high_water, total_count FROM crawl_state WHERE key LIKE ?",
            (f"%{contains}%",),
        ).fetchall()
        return {
            r["key"]: {
                "cursor": r["cursor"], "done": bool(r["done"]),
                "high_water": r["high_water"], "total_count": r["total_count"],
            }
            for r in rows
        }

    def clear_crawl_state(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM crawl_state WHERE key = ?", (key,))

    # ------------------------------------------------------------------ users

    def logins_to_enrich(self, limit: int | None = None, refresh: bool = False) -> list[str]:
        where = "COALESCE(i.user_type, '') != 'Bot'"
        if not refresh:
            where += " AND (u.login IS NULL OR u.discovered_at IS NULL)"
        rows = self._conn.execute(
            f"SELECT DISTINCT i.login FROM interactions i "
            f"LEFT JOIN users u ON u.login = i.login WHERE {where} "
            f"ORDER BY i.login LIMIT ?",
            (-1 if limit is None else limit,),
        ).fetchall()
        return [r["login"] for r in rows]

    def logins_without_email(
        self,
        limit: int | None = None,
        *,
        column: str = "commit_probed_at",
        refresh: bool = False,
    ) -> list[str]:
        """Real users we have a profile for, no email yet, and have not probed via `column`."""
        if column not in ("commit_probed_at", "provider_probed_at"):
            raise ValueError(f"unknown probe column: {column}")
        clause = "" if refresh else f"AND u.{column} IS NULL"
        rows = self._conn.execute(
            f"""
            SELECT u.login FROM users u
            WHERE u.profile_found = 1
              AND COALESCE(u.type, 'User') = 'User'
              AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.login = u.login)
              {clause}
            ORDER BY COALESCE(u.score, 0) DESC, COALESCE(u.followers, 0) DESC, u.login
            LIMIT ?
            """,
            (-1 if limit is None else limit,),
        ).fetchall()
        return [r["login"] for r in rows]

    def upsert_profile(self, p: Profile) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO users (login, type, name, company, bio, location, blog, twitter, "
                "public_email, hireable, followers, profile_found, fetched_at, node_id, "
                "account_created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(login) DO UPDATE SET type=excluded.type, name=excluded.name, "
                "company=excluded.company, bio=excluded.bio, location=excluded.location, "
                "blog=excluded.blog, twitter=excluded.twitter, public_email=excluded.public_email, "
                "hireable=excluded.hireable, followers=excluded.followers, "
                "profile_found=excluded.profile_found, fetched_at=excluded.fetched_at, "
                "node_id=COALESCE(excluded.node_id, users.node_id), "
                "account_created_at=COALESCE(excluded.account_created_at, users.account_created_at)",
                (
                    p.login,
                    p.type,
                    p.name,
                    p.company,
                    p.bio,
                    p.location,
                    p.blog,
                    p.twitter,
                    p.public_email,
                    None if p.hireable is None else int(p.hireable),
                    p.followers,
                    int(p.found),
                    _now(),
                    p.node_id,
                    p.created_at,
                ),
            )

    def mark_discovered(self, login: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE users SET discovered_at = ? WHERE login = ?", (_now(), login)
            )

    def mark_probed(self, logins: Iterable[str], column: str = "commit_probed_at") -> None:
        if column not in ("commit_probed_at", "provider_probed_at", "discovered_at"):
            raise ValueError(f"unknown probe column: {column}")
        data = [(_now(), login) for login in logins]
        if not data:
            return
        with self._conn:
            self._conn.executemany(f"UPDATE users SET {column} = ? WHERE login = ?", data)

    def profiles(self, logins: Sequence[str]) -> dict[str, Profile]:
        """Load stored profiles, keyed by login."""
        if not logins:
            return {}
        marks = ",".join("?" * len(logins))
        out: dict[str, Profile] = {}
        for r in self._conn.execute(
            f"SELECT * FROM users WHERE login IN ({marks})", list(logins)
        ):
            out[r["login"]] = Profile(
                login=r["login"],
                type=r["type"],
                name=r["name"],
                company=r["company"],
                bio=r["bio"],
                location=r["location"],
                blog=r["blog"],
                twitter=r["twitter"],
                public_email=r["public_email"],
                hireable=None if r["hireable"] is None else bool(r["hireable"]),
                followers=r["followers"],
                found=bool(r["profile_found"]),
                node_id=r["node_id"],
                created_at=r["account_created_at"],
            )
        return out

    def set_scores(self, scores: Mapping[str, float]) -> None:
        if not scores:
            return
        with self._conn:
            self._conn.executemany(
                "UPDATE users SET score = ? WHERE login = ?",
                [(float(v), k) for k, v in scores.items()],
            )

    def scoring_inputs(self) -> list[dict[str, Any]]:
        """Everything scoring.py needs, in one pass."""
        rows = self._conn.execute(
            """
            SELECT u.login, u.name, u.company, u.bio, u.location, u.followers,
                   u.account_created_at, u.hireable,
                   (SELECT group_concat(DISTINCT i.kind) FROM interactions i
                      WHERE i.login = u.login) AS kinds,
                   (SELECT COUNT(DISTINCT i.repo) FROM interactions i
                      WHERE i.login = u.login) AS repo_count,
                   (SELECT MAX(i.occurred_at) FROM interactions i
                      WHERE i.login = u.login) AS last_seen,
                   (SELECT MAX(e.confidence) FROM emails e WHERE e.login = u.login) AS email_conf
            FROM users u
            WHERE COALESCE(u.type, 'User') = 'User' AND u.profile_found = 1
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- emails

    def add_emails(self, login: str, candidates: Iterable[EmailCandidate]) -> int:
        """Upsert email candidates. Returns the number of NEW (login, email, source) rows.

        The count is taken *before* writing, because an ``ON CONFLICT DO UPDATE`` also
        bumps ``total_changes`` -- counting changes would report every re-found address
        as new, and inflate the email totals of every scheduled run.
        """
        unique: dict[tuple[str, str], EmailCandidate] = {}
        for c in candidates:
            unique[(c.email, c.source)] = c
        if not unique:
            return 0

        existing = {
            (r["email"], r["source"])
            for r in self._conn.execute(
                "SELECT email, source FROM emails WHERE login = ?", (login,)
            )
        }
        new = sum(1 for key in unique if key not in existing)

        stamp = _now()
        with self._conn:
            self._conn.executemany(
                "INSERT INTO emails (login, email, source, confidence, found_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(login, email, source) DO UPDATE SET "
                "confidence=excluded.confidence, found_at=excluded.found_at",
                [(login, c.email, c.source, c.confidence, stamp) for c in unique.values()],
            )
        return new

    # ------------------------------------------------------------ suppression

    def suppress(self, values: Iterable[str]) -> int:
        data = [(v.strip().lower(), _now()) for v in values if v and v.strip()]
        with self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO suppression (value, added_at) VALUES (?,?)", data
            )
            return self._conn.total_changes - before

    def suppressed(self) -> set[str]:
        return {r["value"] for r in self._conn.execute("SELECT value FROM suppression")}

    # -------------------------------------------------------------- run audit

    def start_run(self, mode: str, targets: str) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (mode, targets, started_at, status) VALUES (?,?,?,'running')",
                (mode, targets, _now()),
            )
        return int(cur.lastrowid or 0)

    def finish_run(self, record: RunRecord) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, interactions_new=?, users_new=?, emails_new=?, "
                "points_used=?, status=?, error=?, extra=? WHERE run_id=?",
                (
                    _now(),
                    record.interactions_new,
                    record.users_new,
                    record.emails_new,
                    record.points_used,
                    record.status,
                    record.error,
                    json.dumps(record.extra) if record.extra else None,
                    record.run_id,
                ),
            )

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
            )
        ]

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self._conn.execute(sql).fetchone()[0]  # noqa: E731
        by_kind = {
            r["kind"]: r["n"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM interactions GROUP BY kind ORDER BY kind"
            )
        }
        by_source = {
            r["source"]: r["n"]
            for r in self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM emails GROUP BY source ORDER BY n DESC"
            )
        }
        unique_users = one("SELECT COUNT(DISTINCT login) FROM interactions")
        with_email = one("SELECT COUNT(DISTINCT login) FROM emails")
        return {
            "repos": one("SELECT COUNT(DISTINCT repo) FROM interactions"),
            "interactions": one("SELECT COUNT(*) FROM interactions"),
            "by_kind": by_kind,
            "unique_users": unique_users,
            "profiles_fetched": one("SELECT COUNT(*) FROM users"),
            # "done looking" = we found an address, or we probed and found none. Users
            # whose email arrived during ingest are never commit-probed, so counting
            # `discovered_at` alone understated this and looked like unfinished work.
            "discovery_done": one(
                "SELECT COUNT(*) FROM users u WHERE u.discovered_at IS NOT NULL "
                "OR EXISTS (SELECT 1 FROM emails e WHERE e.login = u.login)"
            ),
            "discovery_pending": one(
                "SELECT COUNT(*) FROM users u WHERE u.discovered_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.login = u.login) "
                "AND COALESCE(u.type, 'User') = 'User' AND u.profile_found = 1"
            ),
            "users_with_email": with_email,
            "by_source": by_source,
            "hit_rate": round(with_email / unique_users, 3) if unique_users else 0.0,
            "suppressed": one("SELECT COUNT(*) FROM suppression"),
            "runs": one("SELECT COUNT(*) FROM runs"),
        }

    # -------------------------------------------------------------- inspection

    #: Statements allowed by `read_query`. Belt and braces: the connection it opens is
    #: read-only at the SQLite level too, so a write cannot succeed even if it slipped
    #: past this check (`WITH ... DELETE` is valid SQLite, so the prefix alone is not
    #: enough).
    READ_ONLY_STARTS = ("select", "with", "pragma", "explain")

    def table_names(self) -> list[str]:
        return [
            r["name"]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

    def columns(self, table: str) -> list[str]:
        if table not in self.table_names():
            raise ValueError(f"no such table: {table}")
        return [r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")]

    def table_counts(self) -> dict[str, int]:
        return {
            name: self._conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in self.table_names()
        }

    def _read_only_connection(self) -> sqlite3.Connection:
        """A connection SQLite itself refuses to write through."""
        if self.path == ":memory:":
            return self._conn  # in-memory databases cannot be reopened read-only
        uri = f"{Path(self.path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def read_query(
        self, sql: str, params: Sequence[Any] = (), *, limit: int | None = 200
    ) -> list[dict[str, Any]]:
        """Run one read-only statement and return rows as dicts.

        Used by `gitscout db --sql`. Refuses anything that is not a read, and runs
        against a read-only connection so a write is impossible rather than merely
        disallowed.
        """
        statement = sql.strip().rstrip(";").strip()
        if not statement:
            raise ValueError("empty query")
        if ";" in statement:
            raise ValueError("only one statement at a time")
        if not statement.lower().startswith(self.READ_ONLY_STARTS):
            raise ValueError(
                f"only {', '.join(s.upper() for s in self.READ_ONLY_STARTS)} queries are "
                "allowed here"
            )

        conn = self._read_only_connection()
        try:
            cursor = conn.execute(statement, tuple(params))
            rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            raise ValueError(f"query failed: {exc}") from exc
        finally:
            if conn is not self._conn:
                conn.close()

    def table_rows(
        self, table: str, *, limit: int | None = 200, order_by: str | None = None
    ) -> list[dict[str, Any]]:
        """Dump a table. The name is validated against the real schema, never injected."""
        names = self.table_names()
        if table not in names:
            raise ValueError(f"no such table: {table}. Available: {', '.join(names)}")
        sql = f"SELECT * FROM {table}"
        if order_by:
            if order_by not in self.columns(table):
                raise ValueError(f"no column {order_by!r} in {table}")
            sql += f" ORDER BY {order_by} DESC"
        return self.read_query(sql, limit=limit)

    def emails_for(self, login: str) -> list[dict[str, Any]]:
        """Every address found for one person, best first (the CSV shows only the best)."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT email, source, confidence, found_at FROM emails WHERE login = ? "
                "ORDER BY confidence DESC, found_at ASC",
                (login,),
            )
        ]

    # ---------------------------------------------------------------- threads

    #: Mutable on GitHub, so they are refreshed on every re-fetch. `added_at` is
    #: deliberately absent: it records when *we* first saw the thread.
    _THREAD_UPDATABLE = (
        "url", "title", "body", "state", "state_reason", "author", "author_type",
        "author_association", "created_at", "updated_at", "closed_at", "locked",
        "comment_count", "reaction_count", "labels", "assignees", "milestone",
        "participants", "node_id", "comments_stored", "comments_truncated",
        "merged", "merged_at", "merged_by", "is_draft", "additions", "deletions",
        "changed_files", "commits", "base_ref", "head_ref", "review_decision",
        "category", "category_emoji", "is_answerable", "closed", "upvotes",
        "is_answered", "answer_author", "answer_at",
    )

    @staticmethod
    def _thread_values(r: ThreadRecord) -> dict[str, Any]:
        as_json = lambda v: json.dumps(list(v)) if v else None  # noqa: E731
        as_bool = lambda v: None if v is None else int(bool(v))  # noqa: E731
        return {
            "repo": r.repo,
            "kind": r.kind,
            "number": int(r.number),
            "url": r.url,
            "title": r.title,
            "body": r.body,
            "state": r.state,
            "state_reason": r.state_reason,
            "author": r.author,
            "author_type": r.author_type,
            "author_association": r.author_association,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "closed_at": r.closed_at,
            "locked": as_bool(r.locked),
            "comment_count": r.comment_count,
            "reaction_count": r.reaction_count,
            "labels": as_json(r.labels),
            "assignees": as_json(r.assignees),
            "milestone": r.milestone,
            "participants": as_json(r.participants),
            "node_id": r.node_id,
            "comments_stored": int(r.comments_stored),
            "comments_truncated": int(bool(r.comments_truncated)),
            "merged": as_bool(r.merged),
            "merged_at": r.merged_at,
            "merged_by": r.merged_by,
            "is_draft": as_bool(r.is_draft),
            "additions": r.additions,
            "deletions": r.deletions,
            "changed_files": r.changed_files,
            "commits": r.commits,
            "base_ref": r.base_ref,
            "head_ref": r.head_ref,
            "review_decision": r.review_decision,
            "category": r.category,
            "category_emoji": r.category_emoji,
            "is_answerable": as_bool(r.is_answerable),
            "closed": as_bool(r.closed),
            "upvotes": r.upvotes,
            "is_answered": as_bool(r.is_answered),
            "answer_author": r.answer_author,
            "answer_at": r.answer_at,
        }

    def upsert_threads(self, rows: Iterable[ThreadRecord]) -> int:
        """Insert new threads and refresh the ones we already had.

        Returns the number of *new* threads. Done as insert-then-update rather than
        ``ON CONFLICT DO UPDATE`` so the count means what it says: an upsert reports
        one change either way, which would make every re-crawl look like a discovery.
        """
        records = [self._thread_values(r) for r in rows]
        if not records:
            return 0
        stamp = _now()
        cols = ["repo", "kind", "number", *self._THREAD_UPDATABLE]
        insert = (
            f"INSERT OR IGNORE INTO threads ({', '.join(cols)}, added_at, fetched_at) "
            f"VALUES ({', '.join(':' + c for c in cols)}, :stamp, :stamp)"
        )
        update = (
            "UPDATE threads SET "
            + ", ".join(f"{c} = :{c}" for c in self._THREAD_UPDATABLE)
            + ", fetched_at = :stamp "
            "WHERE repo = :repo AND kind = :kind AND number = :number"
        )
        payload = [{**rec, "stamp": stamp} for rec in records]
        with self._conn:
            before = self._conn.total_changes
            self._conn.executemany(insert, payload)
            new = self._conn.total_changes - before
            self._conn.executemany(update, payload)
        return new

    def add_thread_comments(self, rows: Iterable[ThreadComment]) -> int:
        """Insert new comments/reviews/replies, refresh edited ones. Returns new rows."""
        records = [
            {
                "repo": c.repo,
                "kind": c.kind,
                "number": int(c.number),
                "comment_id": c.comment_id,
                "comment_type": c.comment_type,
                "author": c.author,
                "author_type": c.author_type,
                "author_association": c.author_association,
                "body": c.body,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
                "position": int(c.position),
                "is_answer": None if c.is_answer is None else int(bool(c.is_answer)),
                "upvotes": c.upvotes,
                "review_state": c.review_state,
                "parent_id": c.parent_id,
                "url": c.url,
                "file_path": c.file_path,
            }
            for c in rows
        ]
        if not records:
            return 0
        payload = [{**rec, "stamp": _now()} for rec in records]
        with self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO thread_comments "
                "(repo, kind, number, comment_id, comment_type, author, author_type, "
                " author_association, body, created_at, updated_at, position, is_answer, "
                " upvotes, review_state, parent_id, url, file_path, added_at, changed_at) "
                "VALUES (:repo, :kind, :number, :comment_id, :comment_type, :author, "
                ":author_type, :author_association, :body, :created_at, :updated_at, "
                ":position, :is_answer, :upvotes, :review_state, :parent_id, :url, "
                ":file_path, :stamp, :stamp)",
                payload,
            )
            new = self._conn.total_changes - before
            self._conn.executemany(
                "UPDATE thread_comments SET body = :body, updated_at = :updated_at, "
                "author_association = :author_association, position = :position, "
                "is_answer = :is_answer, upvotes = :upvotes, review_state = :review_state, "
                "url = COALESCE(:url, url), file_path = COALESCE(:file_path, file_path), "
                "changed_at = :stamp "
                "WHERE repo = :repo AND kind = :kind AND number = :number "
                "AND comment_id = :comment_id",
                payload,
            )
        return new

    def upsert_repository(self, info: Mapping[str, Any]) -> None:
        """Record one repository's identity and headline numbers."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO repositories (repo, repository_id, node_id, owner, name, url, "
                " description, homepage, stars, forks, language, topics, is_archived, "
                " fetched_at) VALUES (:repo, :repository_id, :node_id, :owner, :name, "
                ":url, :description, :homepage, :stars, :forks, :language, :topics, "
                ":is_archived, :fetched_at) ON CONFLICT(repo) DO UPDATE SET "
                "repository_id=excluded.repository_id, node_id=excluded.node_id, "
                "owner=excluded.owner, name=excluded.name, url=excluded.url, "
                "description=excluded.description, homepage=excluded.homepage, "
                "stars=excluded.stars, forks=excluded.forks, language=excluded.language, "
                "topics=excluded.topics, is_archived=excluded.is_archived, "
                "fetched_at=excluded.fetched_at",
                {**info, "fetched_at": _now()},
            )

    def sync_repositories(self) -> list[dict[str, Any]]:
        out = []
        for r in self._conn.execute("SELECT * FROM repositories ORDER BY repo"):
            rec = dict(r)
            rec["topics"] = json.loads(rec["topics"]) if rec["topics"] else []
            rec["is_archived"] = None if rec["is_archived"] is None else bool(rec["is_archived"])
            out.append(rec)
        return out

    def logins_needing_social(self, limit: int = 0, refresh: bool = False) -> list[str]:
        """Real people whose social links have not been looked up yet."""
        sql = (
            "SELECT login FROM users WHERE profile_found = 1 "
            f"AND COALESCE(type, 'User') NOT IN ({_NON_HUMAN_SQL})"
        )
        if not refresh:
            sql += " AND social_fetched_at IS NULL"
        sql += " ORDER BY login"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["login"] for r in self._conn.execute(sql)]

    def set_social(self, login: str, *, linkedin_url: str | None, public_repos: int | None) -> None:
        """Store what a person publishes about themselves. A missing value stays missing:
        an absent LinkedIn link overwrites nothing that was found before."""
        with self._conn:
            self._conn.execute(
                "UPDATE users SET linkedin_url = COALESCE(?, linkedin_url), "
                "public_repos = COALESCE(?, public_repos), social_fetched_at = ? "
                "WHERE login = ?",
                (linkedin_url, public_repos, _now(), login),
            )

    def thread_participants_without_profile(self, limit: int = 0) -> list[str]:
        """Logins seen in a thread that have no `users` row yet.

        A commenter who never starred, forked or filed anything is invisible to the
        interaction crawl, so nothing ever fetched their profile -- and without a
        profile there is no email. These are exactly the people to look up.
        """
        sql = """
        SELECT DISTINCT login FROM (
            SELECT author AS login, author_type FROM threads
            UNION
            SELECT author, author_type FROM thread_comments
        ) p
        WHERE p.login IS NOT NULL
          AND COALESCE(p.author_type, 'User') NOT IN (""" + _NON_HUMAN_SQL + """)
          AND NOT EXISTS (SELECT 1 FROM users u WHERE u.login = p.login)
        ORDER BY login
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["login"] for r in self._conn.execute(sql)]

    def thread_updated_map(self, repo: str, kind: str) -> dict[int, str | None]:
        """number -> stored ``updated_at``, so an incremental pass can tell a genuinely
        changed thread from one GitHub merely returned again."""
        return {
            int(r["number"]): r["updated_at"]
            for r in self._conn.execute(
                "SELECT number, updated_at FROM threads WHERE repo = ? AND kind = ?",
                (repo, kind),
            )
        }

    def comments_stored(self, repo: str, kind: str, number: int) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM thread_comments "
                "WHERE repo = ? AND kind = ? AND number = ?",
                (repo, kind, int(number)),
            ).fetchone()[0]
        )

    def mark_comments_stored(
        self, repo: str, kind: str, number: int, *, stored: int, truncated: bool
    ) -> None:
        """Correct the comment bookkeeping after a tail fetch, nothing else.

        A full ``upsert_threads`` would need every other field too, and the tail query
        does not ask for them -- passing a partial record would blank the thread.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE threads SET comments_stored = ?, comments_truncated = ?, "
                "fetched_at = ? WHERE repo = ? AND kind = ? AND number = ?",
                (int(stored), int(bool(truncated)), _now(), repo, kind, int(number)),
            )

    def threads_needing_tail(
        self, repo: str | None = None, kind: str | None = None, limit: int = 0
    ) -> list[dict[str, Any]]:
        """Threads longer than the cap, so the newest comments are still missing."""
        sql = (
            "SELECT repo, kind, number, comment_count, comments_stored FROM threads "
            "WHERE comments_truncated = 1"
        )
        params: list[Any] = []
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY comment_count DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(sql, params)]

    # ----------------------------------------------------------------- export

    def export_rows(
        self,
        *,
        repos: Sequence[str] | None = None,
        only_with_email: bool = False,
        min_confidence: float = 0.0,
        new_since: str | None = None,
        order_by_score: bool = False,
    ) -> list[dict[str, Any]]:
        """One row per user: best email + profile + which repos/kinds they touched.

        ``new_since`` keeps only people whose *first* interaction was recorded at or
        after that timestamp -- what a scheduled run exports so each drop is fresh.
        """
        where: list[str] = []
        params: list[Any] = [min_confidence]
        if repos:
            where.append(f"repo IN ({','.join('?' * len(repos))})")
            params.extend(repos)
        repo_filter = f"WHERE {' AND '.join(where)}" if where else ""

        having = ""
        if new_since:
            having = "HAVING MIN(COALESCE(added_at, '')) >= ?"

        order = (
            "ORDER BY (b.email IS NULL), COALESCE(u.score, 0) DESC, b.confidence DESC, a.login"
            if order_by_score
            else "ORDER BY (b.email IS NULL), b.confidence DESC, u.followers DESC, a.login"
        )

        sql = f"""
        WITH best AS (
            SELECT login, email, source, confidence,
                   ROW_NUMBER() OVER (
                       PARTITION BY login ORDER BY confidence DESC, found_at ASC
                   ) AS rn
            FROM emails WHERE confidence >= ?
        ),
        inv AS (
            -- One pass, then a join. Correlated per-row subqueries here would scan
            -- the involvement view once per lead, which is O(users x threads).
            SELECT login,
                   SUM(CASE WHEN kind = 'issues' AND role = 'author'
                            THEN 1 ELSE 0 END) AS issues_authored,
                   SUM(CASE WHEN kind = 'issues' AND role <> 'author'
                            THEN 1 ELSE 0 END) AS issues_commented,
                   MAX(CASE WHEN kind = 'issues' THEN created_at END) AS last_issue_at,
                   SUM(CASE WHEN kind = 'prs' AND role = 'author'
                            THEN 1 ELSE 0 END) AS prs_authored,
                   SUM(CASE WHEN kind = 'prs' AND role = 'commenter'
                            THEN 1 ELSE 0 END) AS prs_commented,
                   SUM(CASE WHEN kind = 'prs' AND role = 'reviewer'
                            THEN 1 ELSE 0 END) AS prs_reviewed,
                   MAX(CASE WHEN kind = 'prs' THEN created_at END) AS last_pr_at,
                   SUM(CASE WHEN kind = 'discussions' AND role = 'author'
                            THEN 1 ELSE 0 END) AS discussions_authored,
                   SUM(CASE WHEN kind = 'discussions' AND role <> 'author'
                            THEN 1 ELSE 0 END) AS discussions_commented,
                   MAX(CASE WHEN kind = 'discussions' THEN created_at END)
                       AS last_discussion_at
            FROM (
                SELECT v.login, v.kind, v.role, t.created_at
                FROM thread_involvement v
                JOIN threads t
                  ON t.repo = v.repo AND t.kind = v.kind AND t.number = v.number
            )
            GROUP BY login
        ),
        ans AS (
            -- Answering someone else's question is the strongest community signal
            -- in the discussions section, so it gets its own column.
            SELECT author AS login, COUNT(*) AS discussions_answered
            FROM thread_comments
            WHERE kind = 'discussions' AND is_answer = 1 AND author IS NOT NULL
            GROUP BY author
        ),
        agg AS (
            SELECT login,
                   group_concat(DISTINCT kind || ':' || repo) AS signals,
                   MIN(occurred_at) AS first_seen,
                   MIN(COALESCE(added_at, '')) AS first_added,
                   MAX(CASE WHEN user_type IN ({_NON_HUMAN_SQL}) THEN 1 ELSE 0 END)
                       AS non_human
            FROM interactions {repo_filter}
            GROUP BY login
            {having}
        )
        SELECT a.login, u.name, b.email, b.source AS email_source,
               b.confidence AS email_confidence, u.company, u.location, u.bio,
               u.blog, u.twitter, u.hireable, u.followers, u.type,
               u.score, u.account_created_at, a.signals, a.first_seen, a.first_added,
               COALESCE(v.issues_authored, 0) AS issues_authored,
               COALESCE(v.issues_commented, 0) AS issues_commented,
               v.last_issue_at,
               COALESCE(v.prs_authored, 0) AS prs_authored,
               COALESCE(v.prs_commented, 0) AS prs_commented,
               COALESCE(v.prs_reviewed, 0) AS prs_reviewed,
               v.last_pr_at,
               COALESCE(v.discussions_authored, 0) AS discussions_authored,
               COALESCE(v.discussions_commented, 0) AS discussions_commented,
               COALESCE(n.discussions_answered, 0) AS discussions_answered,
               v.last_discussion_at
        FROM agg a
        LEFT JOIN users u ON u.login = a.login
        LEFT JOIN best b ON b.login = a.login AND b.rn = 1
        LEFT JOIN inv v ON v.login = a.login
        LEFT JOIN ans n ON n.login = a.login
        WHERE a.non_human = 0 AND COALESCE(u.type, 'User') NOT IN ({_NON_HUMAN_SQL})
        {order}
        """
        if new_since:
            params.append(new_since)

        suppressed = self.suppressed()
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params):
            rec = dict(row)
            if only_with_email and not rec["email"]:
                continue
            if rec["login"].lower() in suppressed:
                continue
            if rec["email"] and rec["email"].lower() in suppressed:
                rec["email"] = rec["email_source"] = rec["email_confidence"] = None
                if only_with_email:
                    continue
            out.append(rec)
        return out

    def thread_rows(
        self,
        *,
        repos: Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
        logins: Sequence[str] | None = None,
        role: str | None = None,
        only_with_email: bool = False,
        min_confidence: float = 0.0,
        with_comments: bool = True,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """One row per (person, thread) they were involved in.

        ``with_comments`` attaches the whole stored thread to each row, so a single
        CSV carries the conversation as well as the item -- author, timestamp and
        body of every comment, plus the subset this person wrote.

        Bots, organizations and suppressed people are excluded, exactly as the lead
        export excludes them: this is lead data, not a repo audit.
        """
        if role and role not in THREAD_ROLES:
            raise ValueError(f"role must be one of {', '.join(THREAD_ROLES)}")
        for kind in kinds or ():
            if kind not in THREAD_KINDS:
                raise ValueError(f"kind must be one of {', '.join(THREAD_KINDS)}")

        where = [f"COALESCE(v.author_type, 'User') NOT IN ({_NON_HUMAN_SQL})"]
        params: list[Any] = [min_confidence]
        if repos:
            where.append(f"v.repo IN ({','.join('?' * len(repos))})")
            params.extend(repos)
        if kinds:
            where.append(f"v.kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if logins:
            where.append(f"v.login IN ({','.join('?' * len(logins))})")
            params.extend(logins)
        if role:
            where.append("v.role = ?")
            params.append(role)

        sql = f"""
        WITH best AS (
            SELECT login, email, source, confidence,
                   ROW_NUMBER() OVER (
                       PARTITION BY login ORDER BY confidence DESC, found_at ASC
                   ) AS rn
            FROM emails WHERE confidence >= ?
        ),
        own AS (
            SELECT repo, kind, number, author AS login, COUNT(*) AS own_comments,
                   MIN(created_at) AS first_comment_at,
                   MAX(created_at) AS last_comment_at,
                   MAX(COALESCE(is_answer, 0)) AS wrote_the_answer
            FROM thread_comments WHERE author IS NOT NULL
            GROUP BY repo, kind, number, author
        )
        SELECT v.login, v.role, v.kind, u.name, b.email, b.source AS email_source,
               b.confidence AS email_confidence, u.score, u.company, u.location,
               t.repo, t.number, t.url, t.title, t.body, t.state, t.state_reason,
               t.author AS thread_author, t.author_association, t.created_at,
               t.updated_at, t.closed_at, t.comment_count, t.reaction_count,
               t.labels, t.assignees, t.milestone, t.participants,
               t.comments_stored, t.comments_truncated,
               t.merged, t.merged_at, t.merged_by, t.is_draft, t.additions,
               t.deletions, t.changed_files, t.commits, t.base_ref, t.head_ref,
               t.review_decision, t.category, t.upvotes, t.is_answered,
               t.answer_author, t.answer_at,
               COALESCE(o.own_comments, 0) AS own_comments,
               o.first_comment_at, o.last_comment_at,
               COALESCE(o.wrote_the_answer, 0) AS wrote_the_answer
        FROM thread_involvement v
        JOIN threads t ON t.repo = v.repo AND t.kind = v.kind AND t.number = v.number
        LEFT JOIN users u ON u.login = v.login
        LEFT JOIN best b ON b.login = v.login AND b.rn = 1
        LEFT JOIN own o
               ON o.repo = v.repo AND o.kind = v.kind AND o.number = v.number
              AND o.login = v.login
        WHERE {' AND '.join(where)}
          AND COALESCE(u.type, 'User') NOT IN ({_NON_HUMAN_SQL})
        ORDER BY COALESCE(u.score, 0) DESC, v.login, t.created_at DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"

        suppressed = self.suppressed()
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params):
            rec = dict(row)
            if rec["login"].lower() in suppressed:
                continue
            if rec["email"] and rec["email"].lower() in suppressed:
                rec["email"] = rec["email_source"] = rec["email_confidence"] = None
            if only_with_email and not rec["email"]:
                continue
            for name in ("labels", "assignees", "participants"):
                rec[name] = json.loads(rec[name]) if rec[name] else []
            for name in ("comments_truncated", "wrote_the_answer"):
                rec[name] = bool(rec[name])
            out.append(rec)

        if with_comments and out:
            self._attach_comments(out)
        return out

    def _attach_comments(self, rows: list[dict[str, Any]]) -> None:
        """Hang each thread's stored comments off its rows, in one query per repo+kind.

        One query per *thread* would be thousands of round trips on a real export, so
        the threads on show are fetched in chunks and handed out by key.
        """
        keys = {(r["repo"], r["kind"], r["number"]) for r in rows}
        threads: dict[tuple[str, str, int], list[dict[str, Any]]] = {k: [] for k in keys}
        by_repo_kind: dict[tuple[str, str], list[int]] = {}
        for repo, kind, number in keys:
            by_repo_kind.setdefault((repo, kind), []).append(number)

        for (repo, kind), numbers in by_repo_kind.items():
            for start in range(0, len(numbers), 500):  # keep well under SQLITE_MAX_VARIABLE_NUMBER
                chunk = numbers[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for row in self._conn.execute(
                    f"SELECT number, comment_type, author, author_association, body, "
                    f"created_at, updated_at, position, is_answer, upvotes, review_state "
                    f"FROM thread_comments "
                    f"WHERE repo = ? AND kind = ? AND number IN ({marks}) "
                    f"ORDER BY number, position, created_at",
                    [repo, kind, *chunk],
                ):
                    rec = dict(row)
                    number = rec.pop("number")
                    rec["is_answer"] = bool(rec["is_answer"]) if rec["is_answer"] is not None else None
                    threads[(repo, kind, number)].append(rec)

        for row in rows:
            key = (row["repo"], row["kind"], row["number"])
            row["comments"] = threads.get(key, [])
            row["own_comment_bodies"] = [
                c["body"] for c in row["comments"] if c["author"] == row["login"] and c["body"]
            ]

    def thread_comment_rows(
        self,
        *,
        repos: Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
        numbers: Sequence[int] | None = None,
        logins: Sequence[str] | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Whole threads, in order. No lead filtering: context includes maintainers."""
        where: list[str] = []
        params: list[Any] = []
        if repos:
            where.append(f"c.repo IN ({','.join('?' * len(repos))})")
            params.extend(repos)
        if kinds:
            where.append(f"c.kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if numbers:
            where.append(f"c.number IN ({','.join('?' * len(numbers))})")
            params.extend(int(n) for n in numbers)
        if logins:
            where.append(f"c.author IN ({','.join('?' * len(logins))})")
            params.extend(logins)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"""
        SELECT c.repo, c.kind, c.number, t.title AS thread_title, t.url AS thread_url,
               c.position, c.comment_id, c.comment_type, c.author, c.author_association,
               c.created_at, c.updated_at, c.is_answer, c.upvotes, c.review_state, c.body
        FROM thread_comments c
        LEFT JOIN threads t
               ON t.repo = c.repo AND t.kind = c.kind AND t.number = c.number
        {clause}
        ORDER BY c.repo, c.kind, c.number, c.position, c.created_at
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(sql, params)]

    def thread_stats(self) -> dict[str, Any]:
        one = lambda sql: self._conn.execute(sql).fetchone()[0]  # noqa: E731
        by_kind = {
            r["kind"]: r["n"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM threads GROUP BY kind ORDER BY kind"
            )
        }
        by_role = {
            r["role"]: r["n"]
            for r in self._conn.execute(
                "SELECT role, COUNT(DISTINCT login) AS n FROM thread_involvement "
                "GROUP BY role ORDER BY role"
            )
        }
        return {
            "threads": one("SELECT COUNT(*) FROM threads"),
            "by_kind": by_kind,
            "thread_repos": one("SELECT COUNT(DISTINCT repo) FROM threads"),
            "comments": one("SELECT COUNT(*) FROM thread_comments"),
            "reviews": one(
                "SELECT COUNT(*) FROM thread_comments WHERE comment_type = 'review'"
            ),
            "people": one("SELECT COUNT(DISTINCT login) FROM thread_involvement"),
            "by_role": by_role,
            "answers": one(
                "SELECT COUNT(*) FROM thread_comments WHERE is_answer = 1"
            ),
            "threads_truncated": one(
                "SELECT COUNT(*) FROM threads WHERE comments_truncated = 1"
            ),
        }

    # ------------------------------------------------------------ remote sync

    #: Timestamp each table changes when *we* touch a row -- not GitHub's own
    #: ``updated_at``, which would make a backfill of old threads look unchanged.
    #: These are what the incremental Supabase push filters on.
    SYNC_WATERMARKS = {
        "users": "fetched_at",
        "emails": "found_at",
        "threads": "fetched_at",
        "thread_comments": "changed_at",
    }

    def sync_leads(
        self, since: str | None = None, *, only_with_email: bool = False, limit: int = 0
    ) -> list[dict[str, Any]]:
        """Person-level rows for the remote mirror: profile + best email + counts.

        Deliberately not ``export_rows``: that one is shaped for a CSV (flattened
        signals, no raw booleans) and has no watermark. This returns typed columns a
        Postgres table can take as-is.

        ``limit`` is applied in SQL, before suppression is applied in Python, so a
        limited call can return slightly fewer rows than asked. It is a debugging
        aid, not a pagination primitive.
        """
        params: list[Any] = []
        where = [f"COALESCE(u.type, 'User') NOT IN ({_NON_HUMAN_SQL})", "u.profile_found = 1"]
        if since:
            # Any enrichment counts as a change, not just a profile refetch: a LinkedIn
            # link or a newly found address must reach the mirror too.
            where.append(
                "MAX(COALESCE(u.fetched_at, ''), COALESCE(u.discovered_at, ''), "
                "COALESCE(u.commit_probed_at, ''), COALESCE(u.social_fetched_at, '')) > ?"
            )
            params.append(since)
        if only_with_email:
            where.append("b.email IS NOT NULL")

        sql = f"""
        WITH best AS (
            SELECT login, email, source, confidence,
                   ROW_NUMBER() OVER (
                       PARTITION BY login ORDER BY confidence DESC, found_at ASC
                   ) AS rn
            FROM emails
        ),
        agg AS (
            SELECT login,
                   COUNT(DISTINCT repo) AS repos_touched,
                   group_concat(DISTINCT kind) AS kinds,
                   MIN(occurred_at) AS first_seen,
                   MAX(occurred_at) AS last_seen
            FROM interactions GROUP BY login
        ),
        inv AS (
            SELECT login,
                   SUM(CASE WHEN kind = 'issues' AND role = 'author'
                            THEN 1 ELSE 0 END) AS issues_authored,
                   SUM(CASE WHEN kind = 'issues' AND role <> 'author'
                            THEN 1 ELSE 0 END) AS issues_commented,
                   SUM(CASE WHEN kind = 'prs' AND role = 'author'
                            THEN 1 ELSE 0 END) AS prs_authored,
                   SUM(CASE WHEN kind = 'prs' AND role = 'commenter'
                            THEN 1 ELSE 0 END) AS prs_commented,
                   SUM(CASE WHEN kind = 'prs' AND role = 'reviewer'
                            THEN 1 ELSE 0 END) AS prs_reviewed,
                   SUM(CASE WHEN kind = 'discussions' AND role = 'author'
                            THEN 1 ELSE 0 END) AS discussions_authored,
                   SUM(CASE WHEN kind = 'discussions' AND role <> 'author'
                            THEN 1 ELSE 0 END) AS discussions_commented
            FROM thread_involvement GROUP BY login
        ),
        ans AS (
            SELECT author AS login, COUNT(*) AS discussions_answered
            FROM thread_comments
            WHERE kind = 'discussions' AND is_answer = 1 AND author IS NOT NULL
            GROUP BY author
        ),
        act AS (
            -- When is this person active, by UTC hour? GitHub strips the committer's
            -- offset and has no timezone field, so this histogram is the only signal
            -- there is -- `estimated_utc_offset` is derived from it in Python.
            SELECT login, json_group_object(hr, n) AS activity_hours_utc
            FROM (
                SELECT login, hr, COUNT(*) AS n FROM (
                    SELECT author AS login, strftime('%H', created_at) AS hr
                    FROM thread_comments WHERE author IS NOT NULL AND created_at IS NOT NULL
                    UNION ALL
                    SELECT author, strftime('%H', created_at)
                    FROM threads WHERE author IS NOT NULL AND created_at IS NOT NULL
                )
                GROUP BY login, hr
            )
            GROUP BY login
        )
        SELECT u.login, u.name, u.company, u.location, u.bio,
               u.blog AS website, u.twitter,
               u.hireable, u.followers, u.score AS lead_score, u.type AS account_type,
               u.account_created_at AS github_joined_at,
               u.fetched_at AS github_fetched_at,
               u.linkedin_url, u.public_repos AS public_repos_count,
               -- SQLite's multi-argument max() is NULL if any argument is, so the
               -- blanks are coalesced away and restored afterwards.
               NULLIF(MAX(COALESCE(u.discovered_at, ''), COALESCE(u.commit_probed_at, ''),
                          COALESCE(u.social_fetched_at, '')), '') AS last_enriched_at,
               -- The exact expression the incremental filter uses, so the watermark
               -- advances past what was just sent instead of re-sending it forever.
               MAX(COALESCE(u.fetched_at, ''), COALESCE(u.discovered_at, ''),
                   COALESCE(u.commit_probed_at, ''), COALESCE(u.social_fetched_at, ''))
                   AS _changed_at,
               b.email AS best_email, b.source AS email_source,
               b.confidence AS email_confidence,
               -- A comment-only participant has no `interactions` row at all, so
               -- every aggregate here is NULL for them. The mirror declares these
               -- NOT NULL, so the zero has to be supplied, not inherited.
               COALESCE(g.repos_touched, 0) AS repos_touched,
               g.kinds AS signals,
               g.first_seen AS first_seen_at, g.last_seen AS last_seen_at,
               a2.activity_hours_utc,
               COALESCE(i.issues_authored, 0) AS issues_opened,
               COALESCE(i.issues_commented, 0) AS issues_commented,
               COALESCE(i.prs_authored, 0) AS prs_opened,
               COALESCE(i.prs_commented, 0) AS prs_commented,
               COALESCE(i.prs_reviewed, 0) AS prs_reviewed,
               COALESCE(i.discussions_authored, 0) AS discussions_opened,
               COALESCE(i.discussions_commented, 0) AS discussions_commented,
               COALESCE(n.discussions_answered, 0) AS discussions_answered
        FROM users u
        LEFT JOIN best b ON b.login = u.login AND b.rn = 1
        LEFT JOIN agg g ON g.login = u.login
        LEFT JOIN inv i ON i.login = u.login
        LEFT JOIN ans n ON n.login = u.login
        LEFT JOIN act a2 ON a2.login = u.login
        WHERE {' AND '.join(where)}
        ORDER BY u.fetched_at
        """
        if limit:
            sql += f" LIMIT {int(limit)}"

        suppressed = self.suppressed()
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params):
            rec = dict(row)
            if rec["login"].lower() in suppressed:
                continue
            if rec["best_email"] and rec["best_email"].lower() in suppressed:
                rec["best_email"] = rec["email_source"] = rec["email_confidence"] = None
            rec["hireable"] = None if rec["hireable"] is None else bool(rec["hireable"])
            rec["signals"] = (
                sorted((rec["signals"] or "").split(",")) if rec["signals"] else []
            )
            hours = json.loads(rec["activity_hours_utc"]) if rec["activity_hours_utc"] else {}
            rec["activity_hours_utc"] = hours or None
            rec["estimated_utc_offset"] = estimate_utc_offset(hours)
            out.append(rec)
        return out

    def sync_emails(self, since: str | None = None, *, limit: int = 0) -> list[dict[str, Any]]:
        """Every discovered address with its provenance, suppressed ones excluded."""
        sql = (
            "SELECT e.login, e.email, e.source, e.confidence, e.found_at FROM emails e "
            "JOIN users u ON u.login = e.login "
            f"WHERE COALESCE(u.type, 'User') NOT IN ({_NON_HUMAN_SQL})"
        )
        params: list[Any] = []
        if since:
            sql += " AND e.found_at > ?"
            params.append(since)
        sql += " ORDER BY e.found_at"
        if limit:
            sql += f" LIMIT {int(limit)}"

        suppressed = self.suppressed()
        return [
            dict(r)
            for r in self._conn.execute(sql, params)
            if r["login"].lower() not in suppressed and r["email"].lower() not in suppressed
        ]

    def sync_threads(self, since: str | None = None, *, limit: int = 0) -> list[dict[str, Any]]:
        """Thread rows, keyed (repo, kind, number) exactly as they are stored here."""
        sql = "SELECT * FROM threads"
        params: list[Any] = []
        if since:
            sql += " WHERE fetched_at > ?"
            params.append(since)
        sql += " ORDER BY fetched_at"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params):
            rec = dict(row)
            for name in ("labels", "assignees", "participants"):
                rec[name] = json.loads(rec[name]) if rec[name] else []
            for name in (
                "locked", "comments_truncated", "merged", "is_draft", "is_answered",
                "is_answerable", "closed",
            ):
                rec[name] = None if rec[name] is None else bool(rec[name])
            out.append(rec)
        return out

    def sync_thread_comments(
        self, since: str | None = None, *, limit: int = 0
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM thread_comments"
        params: list[Any] = []
        if since:
            sql += " WHERE COALESCE(changed_at, added_at) > ?"
            params.append(since)
        sql += " ORDER BY COALESCE(changed_at, added_at)"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params):
            rec = dict(row)
            rec["is_answer"] = None if rec["is_answer"] is None else bool(rec["is_answer"])
            out.append(rec)
        return out

    def sync_interactions(self, since: str | None = None) -> list[dict[str, Any]]:
        """The raw signals -- star, fork, issue, PR, discussion, commit -- one row per
        person per repo per kind. The mirror needs them for per-person fork and
        contribution detail, which the thread tables do not carry."""
        sql = (
            "SELECT i.repo, i.login, i.kind, i.user_type, i.occurred_at, i.extra, "
            "i.added_at FROM interactions i"
        )
        params: list[Any] = []
        if since:
            sql += " WHERE i.added_at > ?"
            params.append(since)
        sql += " ORDER BY i.added_at"
        return [dict(r) for r in self._conn.execute(sql, params)]

    def sync_counts(self, since: str | None = None) -> dict[str, int]:
        """How many rows each table would push.

        It materialises the rows rather than running COUNT(*), because suppression is
        applied in Python -- a SQL count would be off by the suppressed rows and a
        status report that disagrees with the push is worse than a slow one. Measured
        at ~0.9s for 11k leads, which is fine for a command a human runs.
        """
        return {
            "people": len(self.sync_leads(since)),
            "person_emails": len(self.sync_emails(since)),
            "threads": len(self.sync_threads(since)),
            "comments": len(self.sync_thread_comments(since)),
        }
