"""Stage 4: write the leads out, as CSV or JSONL."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

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
    }


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


def export_csv(
    store: Store,
    path: str | Path,
    *,
    repos: Sequence[str] | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    new_since: str | None = None,
    order_by_score: bool = False,
) -> int:
    rows = _rows(
        store,
        repos=repos,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        new_since=new_since,
        order_by_score=order_by_score,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: safe_cell(v) for k, v in to_record(row).items()})
    return len(rows)


def export_jsonl(
    store: Store,
    path: str | Path,
    *,
    repos: Sequence[str] | None = None,
    only_with_email: bool = False,
    min_confidence: float = 0.0,
    new_since: str | None = None,
    order_by_score: bool = False,
) -> int:
    """One JSON object per line. No formula escaping: JSON is not a spreadsheet."""
    rows = _rows(
        store,
        repos=repos,
        only_with_email=only_with_email,
        min_confidence=min_confidence,
        new_since=new_since,
        order_by_score=order_by_score,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(to_record(row), ensure_ascii=False) + "\n")
    return len(rows)


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
