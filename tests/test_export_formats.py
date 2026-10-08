"""Export: JSONL, format inference, scoring order, and the hardening that stays."""
from __future__ import annotations

import csv
import json

import pytest

from gitscout.export import COLUMNS, export_csv, export_jsonl, infer_format, safe_cell, write_export, write_many
from gitscout.models import EmailCandidate, Interaction, Profile
from gitscout.storage import Store


def seeded(store):
    store.add_interactions(
        [
            Interaction("o/r", "alice", "stars", "User", "2024-01-01T00:00:00Z"),
            Interaction("o/r", "bob", "prs", "User", "2024-02-01T00:00:00Z"),
        ]
    )
    store.upsert_profile(
        Profile(
            login="alice",
            type="User",
            name="Alice",
            company="Acme",
            bio="line one\nline two",
            hireable=True,
            followers=12,
        )
    )
    store.upsert_profile(Profile(login="bob", type="User", name="Bob"))
    store.add_emails("alice", [EmailCandidate("alice@acme.dev", "gql_profile", 0.95)])
    store.add_emails("bob", [EmailCandidate("bob@b.dev", "gql_commit", 0.85)])
    store.set_scores({"alice": 10.0, "bob": 90.0})
    return store


def test_infer_format_from_extension_or_override():
    assert infer_format("a.csv") == "csv"
    assert infer_format("a.jsonl") == "jsonl"
    assert infer_format("a.ndjson") == "jsonl"
    assert infer_format("a.txt") == "csv"  # the sensible default
    assert infer_format("a.csv", "jsonl") == "jsonl"
    assert infer_format("a.csv", ".JSONL") == "jsonl"
    with pytest.raises(ValueError, match="format must be"):
        infer_format("a.csv", "parquet")


def test_jsonl_writes_one_object_per_line(tmp_path, store):
    seeded(store)
    out = tmp_path / "leads.jsonl"
    assert export_jsonl(store, out, order_by_score=True) == 2

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert set(records[0]) == set(COLUMNS)
    assert records[0]["profile_url"] == "https://github.com/bob"  # highest score first
    assert records[0]["score"] == 90.0


def test_jsonl_keeps_native_types_not_strings(tmp_path, store):
    seeded(store)
    out = tmp_path / "leads.jsonl"
    export_jsonl(store, out)
    record = next(
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["login"] == "alice"
    )
    assert record["followers"] == 12 and isinstance(record["followers"], int)
    assert record["hireable"] is True
    assert record["email_confidence"] == 0.95


def test_newlines_in_bios_are_flattened_in_both_formats(tmp_path, store):
    seeded(store)
    csv_path, jsonl_path = tmp_path / "a.csv", tmp_path / "a.jsonl"
    export_csv(store, csv_path)
    export_jsonl(store, jsonl_path)

    row = next(r for r in csv.DictReader(csv_path.open(encoding="utf-8")) if r["login"] == "alice")
    assert "\n" not in row["bio"] and "line one line two" == row["bio"]
    record = next(
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["login"] == "alice"
    )
    assert "\n" not in record["bio"]


def test_formula_injection_is_still_neutralised_in_csv(tmp_path, store):
    store.add_interactions([Interaction("o/r", "evil", "stars", "User", "2024-01-01")])
    store.upsert_profile(Profile(login="evil", type="User", name='=cmd|" /C calc"!A0'))
    export_csv(store, tmp_path / "x.csv")
    row = next(csv.DictReader((tmp_path / "x.csv").open(encoding="utf-8")))
    assert row["name"].startswith("'=")


def test_safe_cell_covers_every_dangerous_prefix():
    for prefix in ("=", "+", "-", "@", "\t", "\r"):
        assert safe_cell(prefix + "x").startswith("'")
    assert safe_cell("plain") == "plain"
    assert safe_cell(42) == 42
    assert safe_cell(None) is None


def test_jsonl_does_not_escape_formulas(tmp_path, store):
    """JSON is not a spreadsheet; adding a quote would corrupt the value."""
    store.add_interactions([Interaction("o/r", "evil", "stars", "User", "2024-01-01")])
    store.upsert_profile(Profile(login="evil", type="User", name="=1+1"))
    export_jsonl(store, tmp_path / "x.jsonl")
    record = json.loads((tmp_path / "x.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["name"] == "=1+1"


def test_score_column_is_exported(tmp_path, store):
    seeded(store)
    export_csv(store, tmp_path / "x.csv", order_by_score=True)
    rows = list(csv.DictReader((tmp_path / "x.csv").open(encoding="utf-8")))
    assert [r["login"] for r in rows] == ["bob", "alice"]
    assert rows[0]["score"] == "90.0"


def test_write_export_picks_the_writer_and_reports_it(tmp_path, store):
    seeded(store)
    count, fmt = write_export(store, tmp_path / "a.jsonl")
    assert (count, fmt) == (2, "jsonl")
    count, fmt = write_export(store, tmp_path / "a.csv")
    assert (count, fmt) == (2, "csv")


def test_write_many_writes_the_same_selection_everywhere(tmp_path, store):
    seeded(store)
    results = write_many(
        store, [tmp_path / "a.csv", tmp_path / "a.jsonl"], only_with_email=True
    )
    assert [(c, f) for _, c, f in results] == [(2, "csv"), (2, "jsonl")]
    assert (tmp_path / "a.csv").exists() and (tmp_path / "a.jsonl").exists()


def test_parent_directories_are_created(tmp_path, store):
    seeded(store)
    nested = tmp_path / "deep" / "deeper" / "leads.jsonl"
    assert export_jsonl(store, nested) == 2
    assert nested.exists()


def test_empty_export_still_writes_a_header(tmp_path, store):
    assert export_csv(store, tmp_path / "e.csv") == 0
    assert (tmp_path / "e.csv").read_text(encoding="utf-8").strip() == ",".join(COLUMNS)
    assert export_jsonl(store, tmp_path / "e.jsonl") == 0
    assert (tmp_path / "e.jsonl").read_text(encoding="utf-8") == ""


def test_filters_pass_through_to_both_writers(tmp_path, store):
    seeded(store)
    store.add_interactions([Interaction("o/r", "nomail", "stars", "User", "2024-03-01")])
    store.upsert_profile(Profile(login="nomail", type="User"))

    assert export_csv(store, tmp_path / "a.csv", only_with_email=True) == 2
    assert export_jsonl(store, tmp_path / "a.jsonl", min_confidence=0.9) == 3  # rows kept, email dropped
    assert export_csv(store, tmp_path / "b.csv", only_with_email=True, min_confidence=0.9) == 1
    assert export_csv(store, tmp_path / "c.csv", repos=["o/nope"]) == 0


def test_the_low_level_writers_order_by_confidence_unless_told_otherwise(tmp_path, store):
    """`write_export` defaults to score order; export_csv/jsonl keep the v0.1 default."""
    seeded(store)
    export_csv(store, tmp_path / "conf.csv")
    by_confidence = [r["login"] for r in csv.DictReader((tmp_path / "conf.csv").open(encoding="utf-8"))]
    assert by_confidence == ["alice", "bob"]  # 0.95 before 0.85

    write_export(store, tmp_path / "score.csv")
    by_score = [r["login"] for r in csv.DictReader((tmp_path / "score.csv").open(encoding="utf-8"))]
    assert by_score == ["bob", "alice"]  # score 90 before 10
