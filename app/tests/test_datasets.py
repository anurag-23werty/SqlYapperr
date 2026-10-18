from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.storage.app_db import AppDB
from app.storage.datasets import (
    DatasetService,
    IngestError,
    IngestLimits,
    context_from_meta,
    describe_sqlite,
    ingest_csv_files,
    sanitize_identifier,
)

EMPLOYEES = (
    b"Employee ID,Full Name,Email,Dept,Salary,Join Date\n"
    b'1,Asha Rao,asha@x.com,Engineering,"1,20,000",12/03/2021\n'
    b"2,Ben Li,ben@x.com,Sales,85000,25/11/2020\n"
    b"3,Chen Wu,chen@x.com,Engineering,99000.5,01/01/2022\n"
)
TIMESHEETS = b"employee_id;project;hours\n1;Apollo;12\n2;Zeus;7\n1;Zeus;3\n"


def build(tmp_path: Path, files=None) -> Path:  # type: ignore[no-untyped-def]
    path = tmp_path / "ds.db"
    ingest_csv_files(files or [("employees.csv", EMPLOYEES), ("timesheets 2024.csv", TIMESHEETS)], path, IngestLimits())
    return path


def test_ingest_types_dates_and_indexes(tmp_path: Path) -> None:
    path = build(tmp_path)
    meta = describe_sqlite(path)
    tables = {t["name"]: {c["name"]: c for c in t["columns"]} for t in meta["tables"]}
    assert set(tables) == {"employees", "timesheets_2024"}
    emp = tables["employees"]
    assert emp["employee_id"]["type"] == "INTEGER"
    assert emp["salary"]["type"] == "REAL"
    assert emp["join_date"]["semantic"] == "date"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT join_date, salary FROM employees ORDER BY employee_id").fetchall()[0] == ("2021-03-12", 120000.0)
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")}
    assert any("employee_id" in i for i in indexes) and any("join_date" in i for i in indexes)
    assert {"from": "timesheets_2024.employee_id", "to": "employees.employee_id"} in meta["relationships"]


def test_sensitive_columns_hidden_by_default_and_excluded_from_policy(tmp_path: Path) -> None:
    meta = describe_sqlite(build(tmp_path))
    emp = next(t for t in meta["tables"] if t["name"] == "employees")
    email = next(c for c in emp["columns"] if c["name"] == "email")
    assert email["hidden"] and email["samples"] == [] and email["min"] is None
    ctx = context_from_meta("x", "x", tmp_path / "ds.db", meta)
    assert "email" not in ctx.policy.tables["employees"]
    assert "email" in ctx.physical_columns["employees"]  # known to the guard, never readable


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('name"; DROP TABLE x;--', "name_drop_table_x"),
        ("Order Date", "order_date"),
        ("2024 sales", "c_2024_sales"),
        ("select", "select_col"),
        ("sqlite_master", "sqlite_master_col"),
        ("ÉLAN №1", "elan_no1"),
        ("", "fallback"),
    ],
)
def test_identifier_sanitization(raw: str, expected: str) -> None:
    assert sanitize_identifier(raw, "fallback", set()) == expected


def test_duplicate_headers_get_unique_names(tmp_path: Path) -> None:
    path = build(tmp_path, [("d.csv", b"a,A,a \n1,2,3\n")])
    cols = [c["name"] for c in describe_sqlite(path)["tables"][0]["columns"]]
    assert cols == ["a", "a_2", "a_3"]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ([], "at least one"),
        ([("x.exe", b"a\n1\n")], "not a CSV"),
        ([("e.csv", b"")], "empty"),
        ([("e.csv", b"a,b\n")], "no data rows"),
        ([("e.csv", b"a\x00b\n1\n")], "binary"),
    ],
)
def test_bad_uploads_rejected(tmp_path: Path, files, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(IngestError) as info:
        ingest_csv_files(files, tmp_path / "x.db", IngestLimits())
    assert message in info.value.message


def test_upload_limits(tmp_path: Path) -> None:
    with pytest.raises(IngestError):
        ingest_csv_files([("e.csv", b"a\n" + b"1\n" * 20)], tmp_path / "x.db", IngestLimits(max_rows=10))
    with pytest.raises(IngestError):
        ingest_csv_files([("e.csv", b",".join(b"c%d" % i for i in range(30)) + b"\n" + b"1," * 29 + b"1\n")],
                         tmp_path / "y.db", IngestLimits(max_columns=10))
    assert not (tmp_path / "x.db").exists()


def test_dataset_ownership_and_visibility(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "app.db")
    svc = DatasetService(db, tmp_path / "users", IngestLimits(), max_per_user=2)
    alice, bob = "a" * 32, "b" * 32
    db.create_user(alice, "alice@x.co", "Alice", "h")
    db.create_user(bob, "bob@x.co", "Bob", "h")
    rec = svc.create_from_csv(alice, "HR", [("employees.csv", EMPLOYEES)])
    assert svc.get_context(alice, rec.id) is not None
    assert svc.get_context(bob, rec.id) is None  # other users cannot even see it
    svc.set_hidden(alice, rec.id, "employees", "salary", True)
    assert "salary" not in svc.get_context(alice, rec.id).policy.tables["employees"]
    svc.set_hidden(alice, rec.id, "employees", None, True)
    assert "employees" not in svc.get_context(alice, rec.id).policy.tables
    with pytest.raises(KeyError):
        svc.set_hidden(alice, rec.id, "nope", None, True)
    assert not svc.delete(bob, rec.id)
    assert svc.delete(alice, rec.id) and not Path(rec.db_file).exists()


def test_dataset_quota(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "app.db")
    db.create_user("c" * 32, "c@x.co", "C", "h")
    svc = DatasetService(db, tmp_path / "users", IngestLimits(), max_per_user=1)
    svc.create_from_csv("c" * 32, "one", [("e.csv", b"a\n1\n")])
    with pytest.raises(IngestError):
        svc.create_from_csv("c" * 32, "two", [("e.csv", b"a\n1\n")])


def test_path_traversal_ids_rejected(tmp_path: Path) -> None:
    svc = DatasetService(AppDB(tmp_path / "app.db"), tmp_path / "users", IngestLimits(), max_per_user=5)
    with pytest.raises(IngestError):
        svc._file_for("../../etc", "a" * 32)
