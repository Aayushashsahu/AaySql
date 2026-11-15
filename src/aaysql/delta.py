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

from .errors import ConstraintError, ExecutionError, StorageError
from .json_value import ABSENT, JSON_NULL, canonical_json_text

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
        "alter_collection",
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
    "alter_collection": frozenset({"t", "table", "action", "value"}),
}
_SCALAR_TYPES = (str, int, float, bool, type(None))


def _serialize_delta_row(row: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for k, v in row.items():
        if v is ABSENT:
            raise StorageError(f"ABSENT sentinel cannot be persisted for column '{k}'")
        if v is JSON_NULL:
            serialized[k] = "null"
        elif isinstance(v, (dict, list)):
            serialized[k] = canonical_json_text(v)
        else:
            serialized[k] = v
    return serialized


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
        copied = [_serialize_delta_row(row) for row in rows]
        if copied:
            self._changes.append({"t": "insert", "table": table_name, "rows": copied})

    def record_update(
        self, table_name: str, index: int, row: dict[str, Any]
    ) -> None:
        if not self._recording:
            return
        self._changes.append(
            {
                "t": "update",
                "table": table_name,
                "index": index,
                "row": _serialize_delta_row(row),
            }
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
        entry: dict[str, Any] = {
            "t": "create_table",
            "table": table.name,
            "columns": [_column_dict(column) for column in table.columns],
        }
        if table.is_collection:
            entry["is_collection"] = True
            entry["validation_mode"] = table.validation_mode
            if table.schema is not None:
                entry["schema"] = table.schema
            entry["schema_version"] = table.schema_version
            entry["warn_count"] = table.warn_count
            if table.last_warn is not None:
                entry["last_warn"] = table.last_warn
        self._changes.append(entry)

    def record_drop_table(self, table_name: str) -> None:
        if self._recording:
            self._changes.append({"t": "drop_table", "table": table_name})

    def record_create_index(
        self,
        name: str,
        table_name: str,
        column: str,
        path: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if self._recording:
            entry: dict[str, Any] = {
                "t": "create_index",
                "index": name,
                "table": table_name,
                "column": column,
            }
            if path:
                entry["path"] = list(path)
            self._changes.append(entry)

    def record_drop_index(self, name: str) -> None:
        if self._recording:
            self._changes.append({"t": "drop_index", "index": name})

    def record_alter_collection(
        self,
        table_name: str,
        action: str,
        value: Any,
        schema_version: int | None = None,
    ) -> None:
        if self._recording:
            entry: dict[str, Any] = {
                "t": "alter_collection",
                "table": table_name,
                "action": action,
                "value": value,
            }
            if schema_version is not None:
                entry["schema_version"] = schema_version
            self._changes.append(entry)


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


def _is_valid_row_val(value: Any) -> bool:
    if isinstance(value, _SCALAR_TYPES) and not isinstance(value, bytes):
        return True
    return bool(isinstance(value, (dict, list)))


def _is_scalar_row(payload: Any) -> bool:
    return isinstance(payload, dict) and all(
        _is_valid_row_val(value) for value in payload.values()
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
    keys = set(op.keys())
    if kind == "create_index":
        if not (keys == CHANGE_KEYS["create_index"] or keys == (CHANGE_KEYS["create_index"] | {"path"})):
            raise StorageError("Invalid field set for 'create_index' WAL change")
        if "path" in op:
            path = op["path"]
            if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
                raise StorageError("Invalid path in 'create_index' WAL change")
    elif kind == "create_table":
        allowed = CHANGE_KEYS["create_table"] | {
            "is_collection",
            "validation_mode",
            "schema",
            "schema_version",
            "warn_count",
            "last_warn",
        }
        if not (CHANGE_KEYS["create_table"].issubset(keys) and keys.issubset(allowed)):
            raise StorageError("Invalid field set for 'create_table' WAL change")
    elif kind == "alter_collection":
        allowed = CHANGE_KEYS["alter_collection"] | {"schema_version"}
        if not (CHANGE_KEYS["alter_collection"].issubset(keys) and keys.issubset(allowed)):
            raise StorageError("Invalid field set for 'alter_collection' WAL change")
    elif keys != CHANGE_KEYS[kind]:
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
    elif kind == "alter_collection":
        action = op.get("action")
        if not isinstance(action, str) or action not in ("SET_VALIDATION", "SET_SCHEMA"):
            raise StorageError("Invalid action in 'alter_collection' WAL change")
        if "schema_version" in op and not isinstance(op["schema_version"], int):
            raise StorageError("Invalid schema_version in 'alter_collection' WAL change")
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
        except (ExecutionError, StorageError, ConstraintError) as exc:
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
                table_dict = {"name": name, "columns": op["columns"], "rows": []}
                for k in ("is_collection", "validation_mode", "schema", "schema_version", "warn_count", "last_warn"):
                    if k in op:
                        table_dict[k] = op[k]
                db.tables[name] = Table.from_dict(name, table_dict)
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
                path = op.get("path")
                db.create_index(op["index"], op["table"], op["column"], path=path)
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
        elif kind == "alter_collection":
            table = _table_for(db, op["table"], "alter_collection")
            action = op["action"]
            if action == "SET_VALIDATION":
                table.validation_mode = str(op["value"]).upper()
            elif action == "SET_SCHEMA":
                table.schema = op["value"]
                table.schema_version = op.get("schema_version", table.schema_version + 1)

    for name in touched:
        table = db.tables.get(name)
        if table is None:
            continue
        for index in db.indexes_on(name):
            index.build(table.rows)
    return db
