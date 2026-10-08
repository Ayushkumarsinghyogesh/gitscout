"""SQLite persistence layer (stdlib only).

Layers:  interactions (raw) -> users (profiles) -> emails (discovered, scored)
All writes are idempotent so any stage can be re-run safely.

Two tables exist purely for the scheduled path:

* ``crawl_state`` -- a cursor (for resuming a backfill) *and* a high-water mark (the
  newest interaction already seen) per repo+kind. The high-water mark is what lets a
  cron run read only page 1 and stop.
* ``runs`` -- an audit log, so you can see what each scheduled run actually found.

``checkpoints`` is the REST path's older per-page checkpoint and is left untouched.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import NON_HUMAN_TYPES, EmailCandidate, Interaction, Profile, RunRecord


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
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_interactions_added ON interactions(added_at)",
    "CREATE INDEX IF NOT EXISTS idx_emails_login ON emails(login)",
    "CREATE INDEX IF NOT EXISTS idx_users_score ON users(score)",
)


def _now() -> str:
    """Microseconds, not seconds: `--new-only` compares a run's start against
    `interactions.added_at`, and second resolution made two runs in the same second
    indistinguishable."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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

    def known_logins(self, logins: Sequence[str]) -> set[str]:
        """Which of these logins already have a users row (so are not new)."""
        if not logins:
            return set()
        marks = ",".join("?" * len(logins))
        return {
            r["login"]
            for r in self._conn.execute(
                f"SELECT login FROM users WHERE login IN ({marks})", list(logins)
            )
        }

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

    def last_successful_run_at(self, mode: str | None = None) -> str | None:
        sql = "SELECT started_at FROM runs WHERE status = 'ok'"
        params: list[Any] = []
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        sql += " ORDER BY run_id DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return row["started_at"] if row else None

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
               u.score, u.account_created_at, a.signals, a.first_seen, a.first_added
        FROM agg a
        LEFT JOIN users u ON u.login = a.login
        LEFT JOIN best b ON b.login = a.login AND b.rn = 1
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
