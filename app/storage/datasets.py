"""Turn client data (CSV files) into a queryable, policy-protected SQLite dataset.

Ingestion is trusted code: it parses CSV with the standard library, sanitizes
every identifier, infers column types, writes rows with parameterised inserts,
adds indexes on join/date columns and records statistics. The result is a
per-user SQLite file plus JSON metadata describing tables and columns.

Columns whose names look sensitive (email, phone, password, ...) are hidden by
default. Hidden columns are excluded from the access policy, so neither the
model, the AST guard nor the SQLite authorizer will let anyone read them.
"""

from __future__ import annotations

import csv
import io
import math
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.security.policies import AccessPolicy, build_policy
from app.storage.app_db import AppDB, DatasetRecord

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RESERVED = {
    "select", "from", "where", "group", "order", "by", "limit", "offset", "join", "on", "and", "or", "not",
    "table", "index", "union", "all", "as", "case", "when", "then", "else", "end", "null", "is", "in",
    "having", "distinct", "values", "insert", "update", "delete", "create", "drop", "with", "left", "right",
    "inner", "outer", "cross", "natural", "using", "default", "check", "primary", "key", "references",
}
SENSITIVE = re.compile(
    r"(^|_)(e?mail|email_address|phone|mobile|contact_no|password|passwd|pwd|secret|token|api_?key|ssn|"
    r"aadhaar|aadhar|pan_no|pan_number|passport|credit_card|card_number|cvv|iban|account_number|"
    r"account_no|dob|date_of_birth|birth_date|address|street|zip|zipcode|pincode|ip_address)($|_)"
)
_INT = re.compile(r"^[+-]?\d{1,18}$")
_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?$")  # 1,234,567 and Indian 1,20,000
_CURRENCY = re.compile(r"^([+-]?)\s*[$\u20b9\u20ac\u00a3]\s*")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y")
_DATETIME_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_MAX_SAMPLE_CHARS = 40


class IngestError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class IngestLimits:
    max_files: int = 10
    max_rows: int = 500_000
    max_columns: int = 100
    max_bytes: int = 20 * 1024 * 1024


@dataclass(frozen=True)
class DatasetContext:
    """Everything the agent needs for one dataset. Built from trusted metadata."""

    id: str
    name: str
    db_path: Path
    meta: dict[str, Any]
    policy: AccessPolicy
    physical_columns: dict[str, frozenset[str]]


# ------------------------------------------------------------------ naming


def sanitize_identifier(raw: str, fallback: str, taken: set[str]) -> str:
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"c_{text}"
    if text in _RESERVED or text.startswith("sqlite"):
        text = f"{text}_col"
    text = text[:56]
    candidate, n = text, 2
    while candidate in taken:
        candidate, n = f"{text}_{n}", n + 1
    taken.add(candidate)
    if not IDENTIFIER.fullmatch(candidate):  # defensive: never emit an unsafe identifier
        raise IngestError("could not derive a safe identifier")
    return candidate


def _quote(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier):
        raise IngestError("unsafe identifier")
    return f'"{identifier}"'


# ----------------------------------------------------------------- parsing


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError("file is not valid text")  # pragma: no cover - latin-1 always decodes


def _read_csv(data: bytes, limits: IngestLimits) -> tuple[list[str], list[list[str]]]:
    text = _decode(data)
    if "\x00" in text:
        raise IngestError("file looks binary, not CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise IngestError("file is empty") from None
    if not any(h.strip() for h in header):
        raise IngestError("first row must contain column names")
    if len(header) > limits.max_columns:
        raise IngestError(f"too many columns (max {limits.max_columns})")
    width = len(header)
    rows: list[list[str]] = []
    for row in reader:
        if not row or all(not c.strip() for c in row):
            continue
        if len(rows) >= limits.max_rows:
            raise IngestError(f"too many rows (max {limits.max_rows})")
        rows.append((row + [""] * width)[:width])
    if not rows:
        raise IngestError("file has a header but no data rows")
    return header, rows


def _infer(values: Sequence[str]) -> tuple[str, str | None, str | None]:
    """Return (sqlite_type, semantic, date_format)."""
    present = [v.strip() for v in values if v.strip() != ""]
    if not present:
        return "TEXT", None, None
    # Zero-padded values ("007", "0451") are codes, not numbers: keep them as text.
    if any(re.fullmatch(r"0\d+", v) for v in present):
        return "TEXT", None, None
    numeric = [_numeric_text(v) for v in present]
    if all(n is not None and _INT.match(n) for n in numeric):
        return "INTEGER", None, None
    if all(n is not None and _is_number(n) for n in numeric):
        return "REAL", None, None
    if all(_DATETIME_ISO.match(v) for v in present):
        return "TEXT", "datetime", "iso"
    for fmt in _DATE_FORMATS:
        if all(_parse_date(v, fmt) for v in present):
            return "TEXT", "date", fmt
    return "TEXT", None, None


def _numeric_text(v: str) -> str | None:
    """Strip a leading currency symbol and digit-group commas; None if not numeric-looking."""
    v = _CURRENCY.sub(r"\1", v.strip())
    if _GROUPED.match(v):
        v = v.replace(",", "")
    return v if re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", v) else None


def _is_number(v: str) -> bool:
    try:
        f = float(v)
    except ValueError:
        return False
    return math.isfinite(f)


def _parse_date(v: str, fmt: str) -> bool:
    try:
        datetime.strptime(v, fmt)
        return True
    except ValueError:
        return False


def _convert(value: str, sqlite_type: str, date_format: str | None) -> Any:
    v = value.strip()
    if v == "":
        return None
    if sqlite_type == "INTEGER":
        return int(_numeric_text(v) or v)
    if sqlite_type == "REAL":
        return float(_numeric_text(v) or v)
    if date_format and date_format != "iso":
        return datetime.strptime(v, date_format).strftime("%Y-%m-%d")
    return v


# --------------------------------------------------------------- ingestion


def ingest_csv_files(files: Sequence[tuple[str, bytes]], dest: Path, limits: IngestLimits) -> None:
    """Create a SQLite database at ``dest`` from CSV files (one table per file)."""
    if not files:
        raise IngestError("upload at least one CSV file")
    if len(files) > limits.max_files:
        raise IngestError(f"too many files (max {limits.max_files})")
    if sum(len(b) for _, b in files) > limits.max_bytes:
        raise IngestError(f"upload exceeds {limits.max_bytes // (1024 * 1024)} MB")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        table_names: set[str] = set()
        for index, (filename, data) in enumerate(files, start=1):
            if not filename.lower().endswith((".csv", ".tsv", ".txt")):
                raise IngestError(f"'{_safe_label(filename)}' is not a CSV file")
            header, rows = _read_csv(data, limits)
            stem = Path(filename).stem
            table = sanitize_identifier(stem, f"table_{index}", table_names)
            col_names: set[str] = set()
            columns = [sanitize_identifier(h, f"column_{i}", col_names) for i, h in enumerate(header, start=1)]
            inferred = [_infer([r[i] for r in rows]) for i in range(len(columns))]

            col_sql = ", ".join(f"{_quote(c)} {t}" for c, (t, _, _) in zip(columns, inferred, strict=True))
            conn.execute(f"CREATE TABLE {_quote(table)} ({col_sql})")
            placeholders = ", ".join("?" for _ in columns)
            insert = f"INSERT INTO {_quote(table)} VALUES ({placeholders})"
            batch: list[list[Any]] = []
            for row in rows:
                try:
                    batch.append([_convert(v, t, f) for v, (t, _, f) in zip(row, inferred, strict=True)])
                except ValueError:
                    raise IngestError(f"could not convert a value in '{_safe_label(filename)}'") from None
                if len(batch) >= 5_000:
                    conn.executemany(insert, batch)
                    batch.clear()
            if batch:
                conn.executemany(insert, batch)

            # Index join keys and dates so typical analytical queries can SEARCH instead of SCAN.
            indexed = 0
            for c, (_, semantic, _) in zip(columns, inferred, strict=True):
                if indexed >= 8:
                    break
                if c == "id" or c.endswith("_id") or semantic in {"date", "datetime"}:
                    conn.execute(f"CREATE INDEX {_quote(f'ix_{table}_{c}'[:63])} ON {_quote(table)} ({_quote(c)})")
                    indexed += 1
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _sqly_columns (tbl TEXT, col TEXT, original TEXT, semantic TEXT, "
                "date_format TEXT, source TEXT)"
            )
            conn.executemany(
                "INSERT INTO _sqly_columns VALUES (?, ?, ?, ?, ?, ?)",
                [(table, c, h[:200], s, f, filename[:200]) for c, h, (_, s, f) in zip(columns, header, inferred, strict=True)],
            )
        conn.execute("ANALYZE")
        conn.commit()
    except Exception:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    tmp.replace(dest)


def _safe_label(text: str) -> str:
    return "".join(ch for ch in text if ch.isprintable())[:60]


def _clean_sample(value: Any) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:_MAX_SAMPLE_CHARS]


# --------------------------------------------------------------- describe


def describe_sqlite(
    db_path: Path, *, hidden_tables: Iterable[str] = (), include_tables: Iterable[str] | None = None
) -> dict[str, Any]:
    """Build dataset metadata (schema + statistics) from a SQLite file.

    Runs trusted, fixed queries on identifiers that come from the catalog and
    are re-validated against :data:`IDENTIFIER` before use.
    """
    hidden_tables = {t.lower() for t in hidden_tables}
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        extra: dict[tuple[str, str], tuple[str | None, str | None, str | None, str | None]] = {}
        has_extra = conn.execute(
            "SELECT COUNT(1) FROM sqlite_schema WHERE type = 'table' AND name = '_sqly_columns'"
        ).fetchone()[0]
        if has_extra:
            for t, c, orig, sem, fmt, src in conn.execute("SELECT tbl, col, original, semantic, date_format, source FROM _sqly_columns"):
                extra[(t, c)] = (orig, sem, fmt, src)

        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name <> '_sqly_columns' ORDER BY name"
            )
        ]
        if include_tables is not None:
            wanted = {t.lower() for t in include_tables}
            names = [n for n in names if n.lower() in wanted]

        tables = []
        for table in names:
            if not IDENTIFIER.fullmatch(table.lower()):
                continue  # never describe (or later allow) a table we cannot name safely
            qt = _quote(table.lower())
            row_count = conn.execute(f"SELECT COUNT(1) FROM {qt}").fetchone()[0]
            columns = []
            for cid, name, ctype, *_ in conn.execute("SELECT cid, name, type, pk FROM pragma_table_info(?)", (table,)):
                col = name.lower()
                if not IDENTIFIER.fullmatch(col):
                    continue
                qc = _quote(col)
                orig, sem, fmt, _src = extra.get((table, name), (None, None, None, None))
                nulls, distinct, mn, mx = conn.execute(
                    f"SELECT SUM(CASE WHEN {qc} IS NULL THEN 1 ELSE 0 END), COUNT(DISTINCT {qc}), MIN({qc}), MAX({qc}) FROM {qt}"
                ).fetchone()
                if sem is None and (ctype or "").upper() == "TEXT" and isinstance(mn, str) and re.match(r"^\d{4}-\d{2}-\d{2}", mn or ""):
                    sem = "date"
                if distinct and distinct <= 30 and (ctype or "TEXT").upper() == "TEXT" and sem is None:
                    samples = [
                        _clean_sample(r[0]) for r in conn.execute(
                            f"SELECT {qc} FROM {qt} WHERE {qc} IS NOT NULL GROUP BY {qc} ORDER BY COUNT(1) DESC LIMIT 15"
                        )
                    ]
                    categorical = True
                else:
                    samples = [
                        _clean_sample(r[0]) for r in conn.execute(f"SELECT {qc} FROM {qt} WHERE {qc} IS NOT NULL LIMIT 3")
                    ]
                    categorical = False
                is_sensitive = bool(SENSITIVE.search(col))
                columns.append({
                    "name": col,
                    "original": orig or name,
                    "type": (ctype or "TEXT").upper(),
                    "semantic": sem,
                    "nulls": int(nulls or 0),
                    "distinct": int(distinct or 0),
                    "min": _clean_sample(mn) if not is_sensitive else None,
                    "max": _clean_sample(mx) if not is_sensitive else None,
                    "samples": samples if not is_sensitive else [],
                    "categorical": categorical,
                    "sensitive": is_sensitive,
                    "hidden": is_sensitive,
                })
            tables.append({
                "name": table.lower(),
                "row_count": int(row_count),
                "hidden": table.lower() in hidden_tables,
                "columns": columns,
            })
        return {"tables": tables, "relationships": infer_relationships(tables)}
    finally:
        conn.close()


def infer_relationships(tables: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Join hints: a ``<x>_id`` column in one table matching ``<x>_id`` (or ``id``) elsewhere."""
    rels: list[dict[str, str]] = []
    by_name = {t["name"]: t for t in tables}
    for t in tables:
        for c in t["columns"]:
            name = c["name"]
            if not name.endswith("_id"):
                continue
            for other in tables:
                if other["name"] == t["name"]:
                    continue
                other_cols = {oc["name"] for oc in other["columns"]}
                stem = name[:-3]
                if name in other_cols and (other["name"] in {stem, stem + "s", stem + "es"} or
                                           any(oc["name"] == name and oc.get("distinct") == other["row_count"]
                                               for oc in other["columns"])):
                    rels.append({"from": f"{t['name']}.{name}", "to": f"{other['name']}.{name}"})
                elif "id" in other_cols and other["name"] in {stem, stem + "s", stem + "es"}:
                    rels.append({"from": f"{t['name']}.{name}", "to": f"{other['name']}.id"})
    seen, out = set(), []
    for r in rels:
        key = tuple(sorted((r["from"], r["to"])))
        if key not in seen and r["from"].split(".")[0] in by_name:
            seen.add(key)
            out.append(r)
    return out


def context_from_meta(dataset_id: str, name: str, db_path: Path, meta: dict[str, Any]) -> DatasetContext:
    allowed: dict[str, set[str]] = {}
    physical: dict[str, frozenset[str]] = {}
    for t in meta["tables"]:
        physical[t["name"]] = frozenset(c["name"] for c in t["columns"])
        if t.get("hidden"):
            continue
        visible = {c["name"] for c in t["columns"] if not c.get("hidden")}
        if visible:
            allowed[t["name"]] = visible
    policy = build_policy(allowed)
    return DatasetContext(
        id=dataset_id, name=name, db_path=db_path, meta=meta, policy=policy,
        physical_columns={t: cols for t, cols in physical.items() if t in policy.tables},
    )


def public_schema(meta: dict[str, Any]) -> dict[str, Any]:
    """Schema as shown to the owner in the UI (includes hidden flags, not hidden values)."""
    return {
        "tables": [
            {
                "name": t["name"], "row_count": t["row_count"], "hidden": t.get("hidden", False),
                "columns": [
                    {k: c.get(k) for k in ("name", "original", "type", "semantic", "hidden", "sensitive", "distinct", "nulls")}
                    for c in t["columns"]
                ],
            }
            for t in meta["tables"]
        ],
        "relationships": meta.get("relationships", []),
    }


# ------------------------------------------------------------ repository


class DatasetService:
    def __init__(self, db: AppDB, users_dir: Path, limits: IngestLimits, max_per_user: int) -> None:
        self.db = db
        self.users_dir = Path(users_dir)
        self.limits = limits
        self.max_per_user = max_per_user

    def _file_for(self, user_id: str, dataset_id: str) -> Path:
        # Both ids are server-generated hex UUIDs; validate anyway to rule out path tricks.
        if not (re.fullmatch(r"[0-9a-f]{32}", user_id) and re.fullmatch(r"[0-9a-f]{32}", dataset_id)):
            raise IngestError("invalid identifier")
        return self.users_dir / user_id / f"{dataset_id}.db"

    def _check_quota(self, user_id: str) -> None:
        if self.db.count_datasets(user_id) >= self.max_per_user:
            raise IngestError(f"dataset limit reached (max {self.max_per_user})")

    def create_from_csv(self, user_id: str, name: str, files: Sequence[tuple[str, bytes]]) -> DatasetRecord:
        self._check_quota(user_id)
        dataset_id = uuid.uuid4().hex
        path = self._file_for(user_id, dataset_id)
        ingest_csv_files(files, path, self.limits)
        meta = describe_sqlite(path)
        record = DatasetRecord(dataset_id, user_id, _clean_name(name), str(path), meta, time.time())
        self.db.add_dataset(record)
        return record

    def add_sample(self, user_id: str, sample_db: Path) -> DatasetRecord:
        """Give a new user a ready-to-query copy of the demo shop database."""
        dataset_id = uuid.uuid4().hex
        path = self._file_for(user_id, dataset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_db, path)
        meta = describe_sqlite(path, hidden_tables={"admin_users"})
        record = DatasetRecord(dataset_id, user_id, "Sample shop (demo)", str(path), meta, time.time())
        self.db.add_dataset(record)
        return record

    def get_context(self, user_id: str, dataset_id: str) -> DatasetContext | None:
        record = self.db.get_dataset(user_id, dataset_id)
        if record is None:
            return None
        return context_from_meta(record.id, record.name, Path(record.db_file), record.meta)

    def set_hidden(self, user_id: str, dataset_id: str, table: str, column: str | None, hidden: bool) -> dict[str, Any]:
        record = self.db.get_dataset(user_id, dataset_id)
        if record is None:
            raise KeyError(dataset_id)
        meta = record.meta
        for t in meta["tables"]:
            if t["name"] != table:
                continue
            if column is None:
                t["hidden"] = hidden
                break
            for c in t["columns"]:
                if c["name"] == column:
                    c["hidden"] = hidden
                    if not hidden and c.get("sensitive"):
                        # Owner explicitly un-hid a sensitive column: still never send its values to the LLM.
                        c["samples"], c["min"], c["max"] = [], None, None
                    break
            else:
                raise KeyError(column)
            break
        else:
            raise KeyError(table)
        self.db.update_dataset_meta(user_id, dataset_id, meta)
        return meta

    def delete(self, user_id: str, dataset_id: str) -> bool:
        record = self.db.get_dataset(user_id, dataset_id)
        if record is None:
            return False
        self.db.delete_dataset(user_id, dataset_id)
        Path(record.db_file).unlink(missing_ok=True)
        return True


def _clean_name(name: str) -> str:
    cleaned = " ".join("".join(ch for ch in name if ch.isprintable()).split())[:80]
    return cleaned or "Untitled dataset"
