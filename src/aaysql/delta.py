"""Delta mutation capture and replay for the v4 WAL.

Mutations are recorded at the storage boundary while they happen, so building a
WAL transaction never diffs or serializes the whole database. One recorded change
is one storage-level operation:

    {"t": "insert", "table": "users", "rows": [{...}]}
    {"t": "update", "table": "users", "index": 1, "row": {...}}
    {"t": "delete", "table": "users", "index": 0}
    {"t": "create_table", "table": "logs", "columns": [...]}
    {"t": "drop_table", "table": "logs"}
    {"t": "create_index", "index": "users_email", "table": "users", "column": "email"}
    {"t": "drop_index", "index": "users_email"}

``update`` and ``delete`` address a row by its position in the table's row list at
the moment the operation ran. Changes are replayed in recording order, which
reproduces row order and duplicate rows without a hidden row id.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .errors import ExecutionError, StorageError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .storage import ColumnDef, Database, Table

CHANGE_TYPES = frozenset(
    {
        "insert",
        "update",
        "delete",
        "create_table",
        "drop_table",
        "create_index",
        "drop_index",
    }
)
CHANGE_KEYS: dict[str, frozenset[str]] = {
    "insert": frozenset({"t", "table", "rows"}),
    "update": frozenset({"t", "table", "index", "row"}),
    "delete": frozenset({"t", "table", "index"}),
    "create_table": frozenset({"t", "table", "columns"}),
    "drop_table": frozenset({"t", "table"}),
    "create_index": frozenset({"t", "index", "table", "column"}),
    "drop_index": frozenset({"t", "index"}),
}
_SCALAR_TYPES = (str, int, float, bool, type(None))


class MutationRecorder:
    """Accumulates the delta changes of one or more statements.

    The recorder is deliberately passive: callers attach it only while a mutation
    is being applied and take (or discard) the changes afterwards. Rows are copied
    at capture time, so a later in-place ``UPDATE`` cannot rewrite history.
    """

    def __init__(self) -> None:
        self._changes: list[dict[str, Any]] = []
        self._recording = False

    @property
    def recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """Begin capturing changes (idempotent)."""
        self._recording = True

    def stop(self) -> list[dict[str, Any]]:
        """Stop capturing and return the recorded changes (still retained)."""
        self._recording = False
        return self._changes

    def mark(self) -> int:
        """Position to which a failed statement can roll back its own changes."""
        return len(self._changes)

    def rollback_to(self, mark: int) -> None:
        if not isinstance(mark, int) or mark < 0 or mark > len(self._changes):
            raise ValueError("recorder mark is out of range")
        del self._changes[mark:]

    def clear(self) -> None:
        self._changes.clear()

    def take(self) -> list[dict[str, Any]]:
        """Return the recorded changes and reset the recorder."""
        changes = self._changes
        self._changes = []
        self._recording = False
        return changes

    def __len__(self) -> int:
        return len(self._changes)

    def record_insert(self, table_name: str, rows: Iterable[dict[str, Any]]) -> None:
        if not self._recording:
            return
        copied = [dict(row) for row in rows]
        if copied:
            self._changes.append({"t": "insert", "table": table_name, "rows": copied})

    def record_update(
        self, table_name: str, index: int, row: dict[str, Any]
    ) -> None:
        if not self._recording:
            return
        self._changes.append(
            {"t": "update", "table": table_name, "index": index, "row": dict(row)}
        )

    def record_delete(self, table_name: str, indices: Iterable[int]) -> None:
        if not self._recording:
            return
        ordered = sorted(indices, reverse=True)
        for index in ordered:
            self._changes.append(
                {"t": "delete", "table": table_name, "index": index}
            )

    def record_create_table(self, table: Table) -> None:
        if not self._recording:
            return
        self._changes.append(
            {
                "t": "create_table",
                "table": table.name,
                "columns": [_column_dict(column) for column in table.columns],
            }
        )

    def record_drop_table(self, table_name: str) -> None:
        if self._recording:
            self._changes.append({"t": "drop_table", "table": table_name})

    def record_create_index(self, name: str, table_name: str, column: str) -> None:
        if self._recording:
            self._changes.append(
                {
                    "t": "create_index",
                    "index": name,
                    "table": table_name,
                    "column": column,
                }
            )

    def record_drop_index(self, name: str) -> None:
        if self._recording:
            self._changes.append({"t": "drop_index", "index": name})


def _column_dict(column: ColumnDef) -> dict[str, Any]:
    return {
        "name": column.name,
        "type_name": column.type_name,
        "primary_key": column.primary_key,
        "not_null": column.not_null,
        "unique": column.unique,
        "default": column.default,
        "has_default": column.has_default,
    }


_COLUMN_KEYS = frozenset(
    {
        "name",
        "type_name",
        "primary_key",
        "not_null",
        "unique",
        "default",
        "has_default",
    }
)


def _is_index(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_scalar_row(payload: Any) -> bool:
    return isinstance(payload, dict) and all(
        isinstance(value, _SCALAR_TYPES) and not isinstance(value, bytes)
        for value in payload.values()
    )


def validate_change(op: Any) -> dict[str, Any]:
    """Structurally validate one recorded change.

    Used both before appending a change to the WAL and when replaying one, so a
    malformed change fails closed instead of being half-applied.
    """
    if not isinstance(op, dict):
        raise StorageError("Invalid WAL change: expected an object")
    kind = op.get("t")
    if kind not in CHANGE_TYPES:
        raise StorageError(f"Invalid WAL change type: {kind!r}")
    if set(op.keys()) != CHANGE_KEYS[kind]:
        raise StorageError(f"Invalid field set for '{kind}' WAL change")
    if kind in ("create_index", "drop_index"):
        name = op.get("index")
        if not isinstance(name, str) or not name:
            raise StorageError(f"Invalid index name in '{kind}' WAL change")
    else:
        name = op.get("table")
        if not isinstance(name, str) or not name:
            raise StorageError(f"Invalid table name in '{kind}' WAL change")

    if kind == "insert":
        rows = op["rows"]
        if not isinstance(rows, list) or not rows:
            raise StorageError("Invalid 'insert' WAL change: expected a non-empty row list")
        for row in rows:
            if not _is_scalar_row(row):
                raise StorageError("Invalid row in 'insert' WAL change")
    elif kind == "update":
        if not _is_index(op["index"]):
            raise StorageError("Invalid row index in 'update' WAL change")
        if not _is_scalar_row(op["row"]):
            raise StorageError("Invalid row in 'update' WAL change")
    elif kind == "delete":
        if not _is_index(op["index"]):
            raise StorageError("Invalid row index in 'delete' WAL change")
    elif kind == "create_table":
        columns = op["columns"]
        if not isinstance(columns, list) or not columns:
            raise StorageError("Invalid 'create_table' WAL change: no columns")
        for column in columns:
            if not isinstance(column, dict) or set(column.keys()) != _COLUMN_KEYS:
                raise StorageError("Invalid column metadata in 'create_table' WAL change")
            if not isinstance(column.get("name"), str) or not column["name"]:
                raise StorageError("Invalid column name in 'create_table' WAL change")
    elif kind == "create_index":
        if not isinstance(op["table"], str) or not op["table"]:
            raise StorageError("Invalid table name in 'create_index' WAL change")
        if not isinstance(op["column"], str) or not op["column"]:
            raise StorageError("Invalid column name in 'create_index' WAL change")
    return op


def validate_changes(changes: Any) -> list[dict[str, Any]]:
    """Validate a whole ``changes`` list from a WAL record."""
    if not isinstance(changes, list):
        raise StorageError("Invalid WAL 'write' record: 'changes' must be a list")
    if not changes:
        raise StorageError("Invalid WAL 'write' record: empty 'changes' list")
    return [validate_change(op) for op in changes]


def _materialize_row(
    table: Table, payload: Any, context: str
) -> dict[str, Any]:
    """Coerce one replayed row against the table's column definitions."""
    from .storage import coerce_value  # local import avoids a module cycle

    if not isinstance(payload, dict):
        raise StorageError(f"Invalid row in {context}")
    expected = set(table.column_names)
    if set(payload.keys()) != expected:
        raise StorageError(f"Row columns do not match table '{table.name}' in {context}")
    row: dict[str, Any] = {}
    for column in table.columns:
        try:
            row[column.name] = coerce_value(payload[column.name], column)
        except (ExecutionError, StorageError) as exc:
            raise StorageError(
                f"Invalid value for '{column.name}' in {context}: {exc}"
            ) from exc
    return row


def _table_for(db: Database, name: str, context: str) -> Table:
    try:
        return db.table(name)
    except ExecutionError as exc:
        raise StorageError(f"{context} references unknown table '{name}'") from exc


def apply_changes(db: Database, changes: Any) -> Database:
    """Apply replayed ``changes`` to ``db`` in recorded order.

    (The ``Table`` import below is local to avoid a module import cycle.)

    Index trees on touched tables are rebuilt from authoritative rows afterwards,
    exactly like a load from checkpoint segments, so replay never has to trust a
    persisted tree structure.
    """
    from .storage import Table  # local import avoids a module cycle

    ops = validate_changes(changes)
    touched: set[str] = set()
    for op in ops:
        kind = op["t"]
        if kind == "insert":
            table = _table_for(db, op["table"], "insert")
            rows = [
                _materialize_row(table, payload, "insert")
                for payload in op["rows"]
            ]
            table.rows.extend(rows)
            table.touch()
            touched.add(table.name)
        elif kind == "update":
            table = _table_for(db, op["table"], "update")
            index = op["index"]
            if index >= len(table.rows):
                raise StorageError(
                    f"update index {index} is out of range for table "
                    f"'{table.name}' ({len(table.rows)} rows)"
                )
            table.rows[index] = _materialize_row(table, op["row"], "update")
            table.touch()
            touched.add(table.name)
        elif kind == "delete":
            table = _table_for(db, op["table"], "delete")
            index = op["index"]
            if index >= len(table.rows):
                raise StorageError(
                    f"delete index {index} is out of range for table "
                    f"'{table.name}' ({len(table.rows)} rows)"
                )
            del table.rows[index]
            table.touch()
            touched.add(table.name)
        elif kind == "create_table":
            name = op["table"]
            if name in db.tables:
                raise StorageError(f"Replayed 'create_table' for existing table '{name}'")
            try:
                db.tables[name] = Table.from_dict(
                    name, {"name": name, "columns": op["columns"], "rows": []}
                )
            except StorageError:
                raise
            except (ExecutionError, ValueError) as exc:
                raise StorageError(
                    f"Invalid schema in 'create_table' for '{name}': {exc}"
                ) from exc
        elif kind == "drop_table":
            name = op["table"]
            if name not in db.tables:
                raise StorageError(f"Replayed 'drop_table' for unknown table '{name}'")
            db.drop_table(name)
        elif kind == "create_index":
            try:
                db.create_index(op["index"], op["table"], op["column"])
            except ExecutionError as exc:
                raise StorageError(
                    f"Invalid replayed 'create_index' for '{op['index']}': {exc}"
                ) from exc
        elif kind == "drop_index":
            if op["index"] not in db.indexes:
                raise StorageError(
                    f"Replayed 'drop_index' for unknown index '{op['index']}'"
                )
            db.drop_index(op["index"])

    for name in touched:
        table = db.tables.get(name)
        if table is None:
            continue
        for index in db.indexes_on(name):
            index.build(table.rows)
    return db
