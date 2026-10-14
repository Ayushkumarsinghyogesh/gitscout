"""Stage 4: write the leads out, as CSV or JSONL."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .storage import Store

COLUMNS = [
    "login",
    "profile_url",
    "name",
    "email",
    "email_source",
    "email_confidence",
    "score",
    "company",
    "location",
    "bio",
    "blog",
    "twitter",
    "hireable",
    "followers",
    "signals",
    "first_seen",
    # Per-section involvement, from the thread stage. All zero until
    # `gitscout threads` (or `scout --with-threads`) has run; the titles, bodies and
    # comment threads live in the thread export, which has one row per thread
    # instead of one per person.
    "issues_authored",
    "issues_commented",
    "last_issue_at",
    "prs_authored",
    "prs_commented",
    "prs_reviewed",
    "last_pr_at",
    "discussions_authored",
    "discussions_commented",
    "discussions_answered",
    "last_discussion_at",
]

FORMATS = ("csv", "jsonl")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> Any:
    """Neutralise CSV/Excel formula injection: bios and names are attacker-controlled."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def to_record(row: dict[str, Any]) -> dict[str, Any]:
    """One database row -> one output record."""
    return {
        "login": row["login"],
        "profile_url": f"https://github.com/{row['login']}",
        "name": row["name"],
        "email": row["email"],
        "email_source": row["email_source"],
        "email_confidence": row["email_confidence"],
        "score": row.get("score"),
        "company": row["company"],
        "location": row["location"],
        "bio": (row["bio"] or "").replace("\r", " ").replace("\n", " ") or None,
        "blog": row["blog"],
        "twitter": row["twitter"],
        "hireable": None if row["hireable"] is None else bool(row["hireable"]),
        "followers": row["followers"],
        "signals": row["signals"],
        "first_seen": row["first_seen"],
        "issues_authored": row.get("issues_authored", 0),
        "issues_commented": row.get("issues_commented", 0),
        "last_issue_at": row.get("last_issue_at"),
        "prs_authored": row.get("prs_authored", 0),
        "prs_commented": row.get("prs_commented", 0),
        "prs_reviewed": row.get("prs_reviewed", 0),
        "last_pr_at": row.get("last_pr_at"),
        "discussions_authored": row.get("discussions_authored", 0),
        "discussions_commented": row.get("discussions_commented", 0),
        "discussions_answered": row.get("discussions_answered", 0),
        "last_discussion_at": row.get("last_discussion_at"),
    }


# ------------------------------------------------- per-user section digests

#: Extra columns `--with-issues` (and friends) append to the lead export. They turn
#: the one-row-per-thread detail into one cell per person, so the file stays one row
#: per user while still carrying the titles, descriptions and conversations.
DIGEST_COLUMNS = {
    "issues": ["issues_total", "issues_detail", "issues_json"],
    "prs": ["prs_total", "prs_detail", "prs_json"],
    "discussions": ["discussions_total", "discussions_detail", "discussions_json"],
}

#: Separates one thread from the next inside a digest cell. Three semicolons because
#: prose never contains them and a spreadsheet will not reinterpret them.
THREAD_SEPARATOR = " ;;; "


def digest_columns(sections: Sequence[str]) -> list[str]:
    out: list[str] = []
    for section in sections:
        out.extend(DIGEST_COLUMNS[section])
    return out


def collect_digests(
    store: Store, sections: Sequence[str], *, repos: Sequence[str] | None = None
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """section -> login -> the threads that person was involved in, newest first.

    Built from `thread_rows`, so suppression, bot filtering and the author/commenter/
    reviewer roles are exactly the same as in the per-thread export -- there is one
    definition of "involved", not two.
    """
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for section in sections:
        by_login: dict[str, list[dict[str, Any]]] = {}
        for row in store.thread_rows(kinds=[section], repos=repos, with_comments=True):
            by_login.setdefault(row["login"], []).append(row)
        for threads in by_login.values():
            threads.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        out[section] = by_login
    return out


def _thread_line(thread: dict[str, Any], body_chars: int, comment_chars: int) -> str:
    """One thread as one readable line: what it was, who said what, when."""
    head = f"#{thread['number']} [{thread.get('state') or 'OPEN'}] {thread.get('title') or ''}".strip()
    opened = thread.get("created_at") or "?"
    author = thread.get("thread_author") or "(deleted)"
    meta = f"role={thread.get('role')} opened={opened} by={author}"
    if thread.get("labels"):
        meta += f" labels={','.join(thread['labels'])}"
    body = _flat(thread.get("body"), body_chars) or ""
    comments = thread.get("comments") or []
    conversation = COMMENT_SEPARATOR.join(
        _comment_line(c, comment_chars) for c in comments
    )
    parts = [head, meta, f"desc: {body}"]
    if conversation:
        parts.append(f"comments({len(comments)}): {conversation}")
    elif thread.get("comment_count"):
        parts.append(f"comments({thread['comment_count']}): (not fetched)")
    return " | ".join(parts)


def _thread_json(
    thread: dict[str, Any], body_chars: int = 0, comment_chars: int = 0
) -> dict[str, Any]:
    """The same thread, structured, for whatever reads the file next.

    In CSV mode the same caps apply as to the readable digest. Excel refuses to show
    more than 32,767 characters in a cell and most CSV readers stop at 128 KB, so an
    uncapped JSON blob makes the file unopenable. Use JSONL for full fidelity.
    """
    cut = (lambda v, c: _flat(v, c)) if (body_chars or comment_chars) else (lambda v, c: v)
    return {
        "number": thread["number"],
        "repo": thread["repo"],
        "url": thread.get("url"),
        "title": thread.get("title"),
        "description": cut(thread.get("body"), body_chars),
        "state": thread.get("state"),
        "role": thread.get("role"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "closed_at": thread.get("closed_at"),
        "labels": thread.get("labels") or [],
        "comment_count": thread.get("comment_count"),
        "own_comments": thread.get("own_comments"),
        "comments": [
            {
                "author": c.get("author"),
                "type": c.get("comment_type"),
                "created_at": c.get("created_at"),
                "body": cut(c.get("body"), comment_chars),
                "is_answer": c.get("is_answer"),
                "review_state": c.get("review_state"),
            }
            for c in (thread.get("comments") or [])
        ],
    }


def digest_fields(
    login: str,
    sections: Sequence[str],
    digests: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    *,
    flatten: bool,
    body_chars: int,
    comment_chars: int,
    max_threads: int = 0,
) -> dict[str, Any]:
    """The digest cells for one person, in the order `digest_columns` lists them.

    ``*_total`` always counts everything they were involved in; ``max_threads`` only
    bounds how many are spelled out in the cell, newest first. Without it one
    prolific maintainer can produce a cell no spreadsheet will open.
    """
    out: dict[str, Any] = {}
    for section in sections:
        involved = digests.get(section, {}).get(login, [])
        threads = involved[:max_threads] if max_threads else involved
        total, detail, raw = DIGEST_COLUMNS[section]
        out[total] = len(involved)
        if flatten:
            lines = [_thread_line(t, body_chars, comment_chars) for t in threads]
            if len(threads) < len(involved):
                lines.append(f"(+{len(involved) - len(threads)} more, newest shown first)")
            out[detail] = THREAD_SEPARATOR.join(lines) or None
            out[raw] = (
                json.dumps(
                    [_thread_json(t, body_chars, comment_chars) for t in threads],
                    ensure_ascii=False,
                )
                if threads
                else None
            )
        else:
            # JSONL: nothing is flattened, stringified or capped.
            out[detail] = [_thread_line(t, 0, 0) for t in involved]
            out[raw] = [_thread_json(t) for t in involved]
    return out

def _rows(
    store: Store,
    *,
    repos: Sequence[str] | None,
    only_with_email: bool,
    min_confidence: float,
    new_since: str | None,
    order_by_score: bool,
) -> list[dict[str, Any]]:
    return store.export_rows(
        repos=repos,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        new_since=new_since,
        order_by_score=order_by_score,
    )


def _write_leads(
    store: Store,
    path: str | Path,
    fmt: str,
    *,
    repos: Sequence[str] | None,
    only_with_email: bool,
    min_confidence: float,
    new_since: str | None,
    order_by_score: bool,
    sections: Sequence[str] = (),
    body_chars: int = 0,
    comment_chars: int = 0,
    max_threads: int = 0,
) -> int:
    """One row per person. ``sections`` appends that person's threads as digest cells."""
    rows = _rows(
        store,
        repos=repos,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        new_since=new_since,
        order_by_score=order_by_score,
    )
    digests = collect_digests(store, sections, repos=repos) if sections else {}
    flatten = fmt == "csv"
    records = []
    for row in rows:
        record = to_record(row)
        if sections:
            record.update(
                digest_fields(
                    row["login"], sections, digests,
                    flatten=flatten, body_chars=body_chars,
                    comment_chars=comment_chars, max_threads=max_threads,
                )
            )
        records.append(record)
    return _write(Path(path), COLUMNS + digest_columns(sections), records, fmt)


def export_csv(
    store: Store,
    path: str | Path,
    *,
    repos: Sequence[str] | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    new_since: str | None = None,
    order_by_score: bool = False,
    sections: Sequence[str] = (),
    body_chars: int = 0,
    comment_chars: int = 0,
    max_threads: int = 0,
) -> int:
    return _write_leads(
        store, path, "csv",
        repos=repos, only_with_email=only_with_email, min_confidence=min_confidence,
        new_since=new_since, order_by_score=order_by_score, sections=sections,
        body_chars=body_chars, comment_chars=comment_chars, max_threads=max_threads,
    )


def export_jsonl(
    store: Store,
    path: str | Path,
    *,
    repos: Sequence[str] | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    new_since: str | None = None,
    order_by_score: bool = False,
    sections: Sequence[str] = (),
    body_chars: int = 0,
    comment_chars: int = 0,
    max_threads: int = 0,
) -> int:
    """One JSON object per line. No formula escaping: JSON is not a spreadsheet."""
    return _write_leads(
        store, path, "jsonl",
        repos=repos, only_with_email=only_with_email, min_confidence=min_confidence,
        new_since=new_since, order_by_score=order_by_score, sections=sections,
        body_chars=body_chars, comment_chars=comment_chars, max_threads=max_threads,
    )


def infer_format(path: str | Path, explicit: str | None = None) -> str:
    if explicit:
        fmt = explicit.lower().lstrip(".")
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)} (got {explicit!r})")
        return fmt
    suffix = Path(path).suffix.lower().lstrip(".")
    return "jsonl" if suffix in ("jsonl", "ndjson", "json") else "csv"


def write_export(
    store: Store,
    path: str | Path,
    *,
    fmt: str | None = None,
    repos: Sequence[str] | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    new_since: str | None = None,
    order_by_score: bool = True,
    sections: Sequence[str] = (),
    body_chars: int = 0,
    comment_chars: int = 0,
    max_threads: int = 0,
) -> tuple[int, str]:
    """Write `path` in whichever format its extension implies. Returns (rows, format)."""
    chosen = infer_format(path, fmt)
    writer = export_jsonl if chosen == "jsonl" else export_csv
    count = writer(
        store,
        path,
        repos=repos,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        new_since=new_since,
        order_by_score=order_by_score,
        sections=sections,
        body_chars=body_chars,
        comment_chars=comment_chars,
        max_threads=max_threads,
    )
    return count, chosen


def write_many(
    store: Store,
    paths: Iterable[str | Path],
    **kwargs: Any,
) -> list[tuple[str, int, str]]:
    """Write the same selection to several files (e.g. a CSV and a JSONL)."""
    out: list[tuple[str, int, str]] = []
    for path in paths:
        count, fmt = write_export(store, path, **kwargs)
        out.append((str(path), count, fmt))
    return out


# -------------------------------------------------------------- thread exports

#: One row per (person, thread). The unbounded columns -- `body`, then the thread
#: itself -- come last on purpose, so a spreadsheet stays readable if you never
#: scroll that far right.
THREAD_COLUMNS = [
    "login",
    "profile_url",
    "name",
    "email",
    "email_source",
    "email_confidence",
    "score",
    "company",
    "location",
    "section",
    "role",
    "repo",
    "number",
    "url",
    "title",
    "state",
    "state_reason",
    "created_at",
    "updated_at",
    "closed_at",
    "author_association",
    "thread_author",
    "labels",
    "assignees",
    "milestone",
    "participants",
    "comment_count",
    "own_comments",
    "reaction_count",
    "comments_stored",
    "comments_truncated",
    # pull requests
    "merged",
    "merged_at",
    "merged_by",
    "is_draft",
    "additions",
    "deletions",
    "changed_files",
    "commits",
    "base_ref",
    "head_ref",
    "review_decision",
    # discussions
    "category",
    "upvotes",
    "is_answered",
    "answer_author",
    "answer_at",
    "wrote_the_answer",
    # the conversation
    "body",
    "own_comments_text",
    "comments_text",
    "comments_json",
]

#: One row per comment, review or reply, for reading whole threads.
COMMENT_COLUMNS = [
    "repo",
    "section",
    "number",
    "thread_title",
    "thread_url",
    "position",
    "comment_id",
    "comment_type",
    "author",
    "author_association",
    "created_at",
    "updated_at",
    "is_answer",
    "upvotes",
    "review_state",
    "body",
]

#: Separates comments inside the flattened `comments_text` cell. Chosen because it
#: does not occur in prose and survives a spreadsheet round-trip.
COMMENT_SEPARATOR = " ||| "


def _flat(value: Any, chars: int = 0) -> Any:
    """Collapse newlines so one record stays one spreadsheet row, and optionally cap."""
    if not isinstance(value, str):
        return value
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
    if chars and len(text) > chars:
        text = text[: chars - 1].rstrip() + "…"
    return text or None


def _comment_line(comment: dict[str, Any], chars: int) -> str:
    """`[review APPROVED] alice @ 2026-01-02T00:00:00Z: body` -- one comment, one line."""
    label = comment.get("comment_type") or "comment"
    if comment.get("review_state"):
        label = f"{label} {comment['review_state']}"
    if comment.get("is_answer"):
        label = f"{label} ANSWER"
    body = _flat(comment.get("body"), chars) or ""
    return f"[{label}] {comment.get('author') or '(deleted)'} @ {comment.get('created_at') or '?'}: {body}"


def to_thread_record(
    row: dict[str, Any],
    *,
    flatten: bool = False,
    body_chars: int = 0,
    comment_chars: int = 0,
) -> dict[str, Any]:
    """One `thread_rows` row -> one output record.

    ``flatten`` is for CSV: bodies are multi-paragraph markdown, and a cell holding
    raw newlines turns one thread into a dozen unreadable spreadsheet rows. It also
    renders the thread twice -- once as readable text, once as JSON -- because a CSV
    is read by people *and* parsed by the next tool along. JSONL instead carries the
    comments as a nested array, verbatim: it is not a spreadsheet.
    """
    cap = (lambda v, c=0: _flat(v, c)) if flatten else (lambda v, c=0: v)
    comments = row.get("comments") or []
    record = {
        "login": row["login"],
        "profile_url": f"https://github.com/{row['login']}",
        "name": row.get("name"),
        "email": row.get("email"),
        "email_source": row.get("email_source"),
        "email_confidence": row.get("email_confidence"),
        "score": row.get("score"),
        "company": row.get("company"),
        "location": row.get("location"),
        "section": row["kind"],
        "role": row["role"],
        "repo": row["repo"],
        "number": row["number"],
        "url": row.get("url"),
        "title": cap(row.get("title")),
        "state": row.get("state"),
        "state_reason": row.get("state_reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "closed_at": row.get("closed_at"),
        "author_association": row.get("author_association"),
        "thread_author": row.get("thread_author"),
        "labels": ",".join(row.get("labels") or ()) or None,
        "assignees": ",".join(row.get("assignees") or ()) or None,
        "milestone": row.get("milestone"),
        "participants": ",".join(row.get("participants") or ()) or None,
        "comment_count": row.get("comment_count"),
        "own_comments": row.get("own_comments"),
        "reaction_count": row.get("reaction_count"),
        "comments_stored": row.get("comments_stored"),
        "comments_truncated": bool(row.get("comments_truncated")),
        "merged": row.get("merged"),
        "merged_at": row.get("merged_at"),
        "merged_by": row.get("merged_by"),
        "is_draft": row.get("is_draft"),
        "additions": row.get("additions"),
        "deletions": row.get("deletions"),
        "changed_files": row.get("changed_files"),
        "commits": row.get("commits"),
        "base_ref": row.get("base_ref"),
        "head_ref": row.get("head_ref"),
        "review_decision": row.get("review_decision"),
        "category": row.get("category"),
        "upvotes": row.get("upvotes"),
        "is_answered": row.get("is_answered"),
        "answer_author": row.get("answer_author"),
        "answer_at": row.get("answer_at"),
        "wrote_the_answer": bool(row.get("wrote_the_answer")),
        "body": cap(row.get("body"), body_chars),
    }
    if flatten:
        own = [
            _comment_line(c, comment_chars)
            for c in comments
            if c.get("author") == row["login"]
        ]
        record["own_comments_text"] = COMMENT_SEPARATOR.join(own) or None
        record["comments_text"] = (
            COMMENT_SEPARATOR.join(_comment_line(c, comment_chars) for c in comments) or None
        )
        # The same caps as the readable column. Excel stops at 32,767 characters per
        # cell and most CSV readers at 128 KB, so an uncapped thread dump makes the
        # file unopenable -- one Trivy issue produced a 1.4 MB cell. JSONL below keeps
        # everything, which is where to go for full fidelity.
        record["comments_json"] = (
            json.dumps(
                [
                    {**c, "body": _flat(c.get("body"), comment_chars)}
                    for c in comments
                ],
                ensure_ascii=False,
            )
            if comments
            else None
        )
    else:
        record["own_comments_text"] = row.get("own_comment_bodies") or []
        record["comments"] = comments
    return record


def to_comment_record(row: dict[str, Any], *, flatten: bool = False) -> dict[str, Any]:
    cap = _flat if flatten else (lambda v: v)
    return {
        "repo": row["repo"],
        "section": row["kind"],
        "number": row["number"],
        "thread_title": cap(row.get("thread_title")),
        "thread_url": row.get("thread_url"),
        "position": row.get("position"),
        "comment_id": row.get("comment_id"),
        "comment_type": row.get("comment_type"),
        "author": row.get("author"),
        "author_association": row.get("author_association"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "is_answer": row.get("is_answer"),
        "upvotes": row.get("upvotes"),
        "review_state": row.get("review_state"),
        "body": cap(row.get("body")),
    }


def _write(
    path: Path, columns: Sequence[str], records: Sequence[dict[str, Any]], fmt: str
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    else:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(columns))
            writer.writeheader()
            for rec in records:
                writer.writerow({k: safe_cell(v) for k, v in rec.items()})
    return len(records)


def write_threads(
    store: Store,
    path: str | Path,
    *,
    fmt: str | None = None,
    repos: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    logins: Sequence[str] | None = None,
    role: str | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    body_chars: int = 0,
    comment_chars: int = 0,
    limit: int = 0,
) -> tuple[int, str]:
    """One row per (person, thread they were involved in). Returns (rows, format)."""
    chosen = infer_format(path, fmt)
    rows = store.thread_rows(
        repos=repos,
        kinds=kinds,
        logins=logins,
        role=role,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        with_comments=True,
        limit=limit,
    )
    records = [
        to_thread_record(
            r, flatten=chosen == "csv", body_chars=body_chars, comment_chars=comment_chars
        )
        for r in rows
    ]
    return _write(Path(path), THREAD_COLUMNS, records, chosen), chosen


def write_thread_comments(
    store: Store,
    path: str | Path,
    *,
    fmt: str | None = None,
    repos: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    logins: Sequence[str] | None = None,
    limit: int = 0,
) -> tuple[int, str]:
    """Write whole threads, in order. Returns (rows, format)."""
    chosen = infer_format(path, fmt)
    rows = store.thread_comment_rows(repos=repos, kinds=kinds, logins=logins, limit=limit)
    records = [to_comment_record(r, flatten=chosen == "csv") for r in rows]
    return _write(Path(path), COMMENT_COLUMNS, records, chosen), chosen
