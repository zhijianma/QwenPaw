# -*- coding: utf-8 -*-
"""Shared SQLite connection and structural validation helpers."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator, Literal


BUSY_TIMEOUT_MS = 5_000


class ChatStorageSchemaError(sqlite3.DatabaseError):
    """A Chat database is valid SQLite but violates its schema contract."""


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back and always close after a context block."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(frozen=True)
class ColumnContract:
    """Required SQLite column structure."""

    storage_type: str
    not_null: bool
    primary_key_position: int = 0


@dataclass(frozen=True)
class IndexContract:
    """Required named index structure."""

    table: str
    columns: tuple[str, ...]
    unique: bool = False
    partial: bool = False


@dataclass(frozen=True)
class ForeignKeyContract:
    """Required single-column foreign key structure."""

    table: str
    column: str
    target_table: str
    target_column: str
    on_delete: str


def connect_database(
    path: Path,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    """Open one consistently configured Chat SQLite connection."""
    if readonly:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            factory=_ClosingConnection,
        )
    else:
        connection = sqlite3.connect(
            path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            factory=_ClosingConnection,
        )
    configured = False
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not readonly:
            mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0],
            ).lower()
            if mode != "wal":
                raise ChatStorageSchemaError(
                    f"Chat database did not enter WAL mode: {mode}",
                )
            connection.execute("PRAGMA synchronous = NORMAL")
        configured = True
        return connection
    finally:
        if not configured:
            connection.close()


@contextmanager
def immediate_transaction(
    connection: sqlite3.Connection,
) -> Iterator[None]:
    """Run a short transaction that acquires the writer lock first."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def validate_integrity(connection: sqlite3.Connection) -> None:
    """Reject corruption and dangling foreign keys."""
    row = connection.execute("PRAGMA quick_check").fetchone()
    result = str(row[0]) if row else "no result"
    if result != "ok":
        raise ChatStorageSchemaError(f"quick_check failed: {result}")
    foreign_key_rows = connection.execute(
        "PRAGMA foreign_key_check",
    ).fetchall()
    if foreign_key_rows:
        raise ChatStorageSchemaError(
            f"foreign_key_check failed: {len(foreign_key_rows)} row(s)",
        )


def validate_user_version(
    connection: sqlite3.Connection,
    *,
    expected: int,
    database_name: str,
) -> None:
    """Require the exact implemented schema version."""
    row = connection.execute("PRAGMA user_version").fetchone()
    actual = int(row[0] if row else 0)
    if actual != expected:
        raise ChatStorageSchemaError(
            f"{database_name} schema version {actual} is unsupported; "
            f"expected {expected}",
        )


def user_tables(connection: sqlite3.Connection) -> set[str]:
    """Return non-SQLite tables, including FTS shadow tables."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'",
    ).fetchall()
    return {str(row["name"]) for row in rows}


def validate_table(
    connection: sqlite3.Connection,
    table: str,
    contract: dict[str, ColumnContract],
) -> None:
    """Validate required column types, nullability, and primary-key order."""
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not rows:
        raise ChatStorageSchemaError(f"Missing required table: {table}")
    actual = {str(row["name"]): row for row in rows}
    missing = set(contract) - set(actual)
    if missing:
        raise ChatStorageSchemaError(
            f"Table {table} is missing column(s): "
            f"{', '.join(sorted(missing))}",
        )
    actual_order = tuple(
        str(row["name"]) for row in rows if str(row["name"]) in contract
    )
    expected_order = tuple(contract)
    if actual_order != expected_order:
        raise ChatStorageSchemaError(
            f"Table {table} column order {actual_order}; expected "
            f"{expected_order}",
        )
    for name, expected in contract.items():
        row = actual[name]
        storage_type = str(row["type"]).upper()
        not_null = bool(row["notnull"])
        primary_key = int(row["pk"])
        if storage_type != expected.storage_type.upper():
            raise ChatStorageSchemaError(
                f"Table {table}.{name} has type {storage_type}; "
                f"expected {expected.storage_type}",
            )
        if not_null != expected.not_null:
            raise ChatStorageSchemaError(
                f"Table {table}.{name} nullability is invalid",
            )
        if primary_key != expected.primary_key_position:
            raise ChatStorageSchemaError(
                f"Table {table}.{name} primary-key position "
                f"{primary_key}; expected {expected.primary_key_position}",
            )


def validate_table_sql(
    connection: sqlite3.Connection,
    table: str,
    required_fragments: tuple[str, ...],
) -> None:
    """Require behavioral constraints that SQLite pragmas do not expose."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or row["sql"] is None:
        raise ChatStorageSchemaError(f"Missing SQL definition for {table}")
    normalized = _normalize_sql(str(row["sql"]))
    for fragment in required_fragments:
        expected = _normalize_sql(fragment)
        if expected not in normalized:
            raise ChatStorageSchemaError(
                f"Table {table} is missing constraint: {fragment}",
            )


def _normalize_sql(value: str) -> str:
    """Normalize insignificant whitespace for schema-fragment checks."""
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    return re.sub(r"\s*([(),])\s*", r"\1", normalized)


def validate_index(
    connection: sqlite3.Connection,
    contract: IndexContract,
    *,
    name: str,
) -> None:
    """Validate one named index including uniqueness and partial status."""
    rows = connection.execute(
        f'PRAGMA index_list("{contract.table}")',
    ).fetchall()
    row = next(
        (item for item in rows if str(item["name"]) == name),
        None,
    )
    if row is None:
        raise ChatStorageSchemaError(
            f"Missing required index on {contract.table}: "
            f"{contract.columns}",
        )
    columns = tuple(
        str(item["name"])
        for item in connection.execute(
            f'PRAGMA index_info("{name}")',
        ).fetchall()
    )
    if columns != contract.columns:
        raise ChatStorageSchemaError(
            f"Index {name} has columns {columns}; expected "
            f"{contract.columns}",
        )
    if bool(row["unique"]) != contract.unique:
        raise ChatStorageSchemaError(f"Index {name} uniqueness is invalid")
    if bool(row["partial"]) != contract.partial:
        raise ChatStorageSchemaError(f"Index {name} partial flag is invalid")


def validate_foreign_key(
    connection: sqlite3.Connection,
    contract: ForeignKeyContract,
) -> None:
    """Validate one single-column foreign-key edge."""
    rows = connection.execute(
        f'PRAGMA foreign_key_list("{contract.table}")',
    ).fetchall()
    found = any(
        str(row["from"]) == contract.column
        and str(row["table"]) == contract.target_table
        and str(row["to"]) == contract.target_column
        and str(row["on_delete"]).upper() == contract.on_delete.upper()
        for row in rows
    )
    if not found:
        raise ChatStorageSchemaError(
            f"Missing foreign key {contract.table}.{contract.column} -> "
            f"{contract.target_table}.{contract.target_column} "
            f"ON DELETE {contract.on_delete}",
        )
