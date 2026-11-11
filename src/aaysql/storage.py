from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ast import ColumnDef
from .btree import BTreeError
from .errors import ConstraintError, ExecutionError, StorageError
from .index import INDEX_NAME_PATTERN, Index
from .statistics import StatisticsStore
from .wal import _CRASH_ENV_VAR, WalManager, _maybe_crash, fsync_dir, parse_wal

FORMAT_NAME = "aaysql"
FORMAT_VERSION = 3
# Format versions 1 (pre-index) and 2 (pre-WAL) remain readable and upgrade to
# version 3 on the next write or recovery checkpoint.
READABLE_FORMAT_VERSIONS = (1, 2, 3)
ENGINE_VERSION = "0.7.0"

TYPE_MAP: dict[str, type] = {
    "INT": int,
    "INTEGER": int,
    "TEXT": str,
    "VARCHAR": str,
    "FLOAT": float,
    "DOUBLE": float,
    "BOOL": bool,
    "BOOLEAN": bool,
}

SUPPORTED_TYPES: tuple[str, ...] = tuple(TYPE_MAP.keys())

_SCALAR_TYPES = (type(None), bool, int, float, str)


def validate_type_name(type_name: str) -> str:
    """Validate and normalize a SQL column data type name."""
    if not isinstance(type_name, str) or not type_name.strip():
        raise ExecutionError("Column data type must be a non-empty string")
    normalized = type_name.strip().upper()
    if normalized not in TYPE_MAP:
        raise ExecutionError(
            f"Unsupported data type '{type_name}'. Supported types: {', '.join(SUPPORTED_TYPES)}"
        )
    return normalized


def coerce_value(value: Any, col: ColumnDef) -> Any:
    """Validate and coerce a value against a column definition and nullability."""
    if value is None:
        if col.not_null or col.primary_key:
            raise ConstraintError(f"Column '{col.name}' cannot be NULL")
        return None
    typ = TYPE_MAP.get(col.type_name.upper())
    if typ is None:
        raise ExecutionError(
            f"Unsupported data type '{col.type_name}' for column '{col.name}'"
        )
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ConstraintError(f"Invalid BOOL value for '{col.name}'")
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConstraintError(f"Column '{col.name}' expects INT")
    elif typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConstraintError(f"Column '{col.name}' expects FLOAT")
        value = float(value)
    elif typ is str:
        if not isinstance(value, str):
            raise ConstraintError(f"Column '{col.name}' expects TEXT")
    return value


def validate_column_def(col: ColumnDef) -> ColumnDef:
    """Validate column type, constraints, and default value compatibility."""
    if not isinstance(col.name, str) or not col.name:
        raise ExecutionError("Column name must be a non-empty string")
    normalized_type = validate_type_name(col.type_name)
    normalized_col = ColumnDef(
        name=col.name,
        type_name=normalized_type,
        primary_key=col.primary_key,
        not_null=col.not_null,
        unique=col.unique,
        default=col.default,
        has_default=col.has_default,
    )
    if col.has_default:
        if not isinstance(col.default, _SCALAR_TYPES):
            raise ConstraintError(f"Invalid DEFAULT value for column '{col.name}'")
        coerced_default = coerce_value(col.default, normalized_col)
        normalized_col = ColumnDef(
            name=col.name,
            type_name=normalized_type,
            primary_key=col.primary_key,
            not_null=col.not_null,
            unique=col.unique,
            default=coerced_default,
            has_default=True,
        )
    elif col.default is not None:
        raise ConstraintError(f"Column '{col.name}' has a default value without has_default=True")
    return normalized_col


def validate_table_schema(columns: list[ColumnDef]) -> list[ColumnDef]:
    """Validate and normalize a full table column schema."""
    if not columns:
        raise ExecutionError("A table must have at least one column")
    names = [c.name for c in columns]
    if len(set(n.lower() for n in names)) != len(names):
        raise ExecutionError("Duplicate column name")
    if sum(c.primary_key for c in columns) > 1:
        raise ExecutionError("Only one PRIMARY KEY column is supported")
    return [validate_column_def(col) for col in columns]


def _column_to_dict(col: ColumnDef) -> dict[str, Any]:
    return {
        "name": col.name,
        "type_name": col.type_name,
        "primary_key": col.primary_key,
        "not_null": col.not_null,
        "unique": col.unique,
        "default": col.default,
        "has_default": col.has_default,
    }


def _column_from_dict(data: Any, table_name: str) -> ColumnDef:
    if not isinstance(data, dict):
        raise StorageError(f"Invalid column metadata in table '{table_name}'")
    required_bools = ("primary_key", "not_null", "unique", "has_default")
    name = data.get("name")
    type_name = data.get("type_name")
    if not isinstance(name, str) or not name:
        raise StorageError(f"Invalid column name in table '{table_name}'")
    if not isinstance(type_name, str) or not type_name:
        raise StorageError(f"Invalid type_name for column '{name}' in table '{table_name}'")
    for key in required_bools:
        if not isinstance(data.get(key), bool):
            raise StorageError(f"Invalid '{key}' flag for column '{name}' in table '{table_name}'")
    if "default" not in data:
        raise StorageError(f"Missing 'default' field for column '{name}' in table '{table_name}'")
    default = data["default"]
    if not isinstance(default, _SCALAR_TYPES):
        raise StorageError(f"Invalid default value for column '{name}' in table '{table_name}'")
    return ColumnDef(
        name=name,
        type_name=type_name,
        primary_key=data["primary_key"],
        not_null=data["not_null"],
        unique=data["unique"],
        default=default,
        has_default=data["has_default"],
    )


@dataclass
class Table:
    name: str
    columns: list[ColumnDef]
    rows: list[dict[str, Any]] = field(default_factory=list)
    # Bumped on every row mutation so cached planner statistics cannot go stale.
    # Not persisted: it is a runtime cache key, not part of the file format.
    revision: int = 0

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def touch(self) -> None:
        """Record that this table's rows changed."""
        self.revision += 1

    def ensure_column(self, name: str) -> None:
        if name not in self.column_names:
            raise ExecutionError(f"Unknown column '{name}' in table '{self.name}'")

    def clone(self) -> Table:
        return Table(
            name=self.name,
            columns=list(self.columns),
            rows=[row.copy() for row in self.rows],
            revision=self.revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [_column_to_dict(col) for col in self.columns],
            "rows": [dict(row) for row in self.rows],
        }

    @classmethod
    def from_dict(cls, key: str, data: Any) -> Table:
        if not isinstance(data, dict):
            raise StorageError(f"Invalid table entry for '{key}'")
        name = data.get("name")
        if not isinstance(name, str) or not name or name != key:
            raise StorageError(f"Mismatched or invalid table name for '{key}'")
        raw_columns = data.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise StorageError(f"Table '{name}' must have a non-empty column list")
        parsed_columns = [_column_from_dict(c, name) for c in raw_columns]
        try:
            columns = validate_table_schema(parsed_columns)
        except (ConstraintError, ExecutionError) as exc:
            raise StorageError(f"Corrupted schema in table '{name}': {exc}") from exc
        col_names = [c.name for c in columns]

        raw_rows = data.get("rows")
        if not isinstance(raw_rows, list):
            raise StorageError(f"Invalid rows list in table '{name}'")

        expected_keys = set(col_names)
        seen_unique: dict[str, set[tuple[type, Any]]] = {
            c.name: set() for c in columns if c.primary_key or c.unique
        }
        rows: list[dict[str, Any]] = []
        for idx, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, dict):
                raise StorageError(f"Invalid row at index {idx} in table '{name}'")
            if set(raw_row.keys()) != expected_keys:
                raise StorageError(f"Row {idx} columns do not match schema for table '{name}'")
            clean_row: dict[str, Any] = {}
            for col in columns:
                try:
                    val = coerce_value(raw_row[col.name], col)
                except (ConstraintError, ExecutionError) as exc:
                    raise StorageError(f"Corrupted row {idx} in table '{name}': {exc}") from exc
                if (col.primary_key or col.unique) and val is not None:
                    key_val = (type(val), val)
                    if key_val in seen_unique[col.name]:
                        kind = "PRIMARY KEY" if col.primary_key else "UNIQUE"
                        raise StorageError(
                            f"Corrupted table '{name}': duplicate {kind} value on '{col.name}'"
                        )
                    seen_unique[col.name].add(key_val)
                clean_row[col.name] = val
            rows.append(clean_row)

        return cls(name=name, columns=columns, rows=rows)


class Database:
    def __init__(self):
        self.tables: dict[str, Table] = {}
        # Index definitions plus their runtime B+ trees. Trees are rebuilt from
        # table rows whenever a database is loaded.
        self.indexes: dict[str, Index] = {}
        self.wal_lsn: int = 0
        # Planner statistics cache. A clone/reload starts empty, so snapshot
        # installation (transaction rollback, WAL recovery) can never expose
        # statistics that describe a superseded row set.
        self.stats = StatisticsStore()

    def create_table(self, name: str, columns: list[ColumnDef]) -> None:
        if name in self.tables:
            raise ExecutionError(f"Table '{name}' already exists")
        validated_columns = validate_table_schema(columns)
        self.tables[name] = Table(name, validated_columns)

    def table(self, name: str) -> Table:
        if name not in self.tables:
            raise ExecutionError(f"Table '{name}' does not exist")
        return self.tables[name]

    def drop_table(self, name: str) -> None:
        self.table(name)
        del self.tables[name]
        self.stats.invalidate(name)
        for index_name in [n for n, index in self.indexes.items() if index.table == name]:
            del self.indexes[index_name]

    def create_index(self, name: str, table_name: str, column: str) -> Index:
        if name in self.indexes:
            raise ExecutionError(f"Index '{name}' already exists")
        table = self.table(table_name)
        table.ensure_column(column)
        index = Index(name, table_name, column, table.rows)
        self.indexes[name] = index
        return index

    def drop_index(self, name: str) -> None:
        if name not in self.indexes:
            raise ExecutionError(f"Index '{name}' does not exist")
        del self.indexes[name]

    def indexes_on(self, table_name: str) -> list[Index]:
        return [index for index in self.indexes.values() if index.table == table_name]

    def clone(self) -> Database:
        copy = Database()
        copy.wal_lsn = self.wal_lsn
        copy.tables = {name: tbl.clone() for name, tbl in self.tables.items()}
        for name, index in self.indexes.items():
            table = copy.tables.get(index.table)
            rows = table.rows if table is not None else ()
            copy.indexes[name] = Index(name, index.table, index.column, rows)
        return copy

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "engine_version": ENGINE_VERSION,
            "wal_lsn": self.wal_lsn,
            "tables": {name: tbl.to_dict() for name, tbl in self.tables.items()},
            "indexes": {
                name: index.describe() for name, index in sorted(self.indexes.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Any) -> Database:
        if not isinstance(data, dict):
            raise StorageError("Database file root must be a JSON object")
        if data.get("format") != FORMAT_NAME:
            raise StorageError(f"Unsupported database format: {data.get('format')!r}")
        version = data.get("format_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise StorageError("Invalid or missing 'format_version'")
        if version not in READABLE_FORMAT_VERSIONS:
            raise StorageError(
                f"Unsupported database format version: {version} "
                f"(supported: {', '.join(str(v) for v in READABLE_FORMAT_VERSIONS)})"
            )
        if version in (1, 2):
            wal_lsn = data.get("wal_lsn", 0)
        else:
            if "wal_lsn" not in data:
                raise StorageError("Missing 'wal_lsn' in version 3 database file")
            wal_lsn = data["wal_lsn"]
        if isinstance(wal_lsn, bool) or not isinstance(wal_lsn, int) or wal_lsn < 0:
            raise StorageError("Invalid 'wal_lsn' in database file")

        raw_tables = data.get("tables")
        if not isinstance(raw_tables, dict):
            raise StorageError("Invalid or missing 'tables' mapping")
        db = cls()
        db.wal_lsn = wal_lsn
        for key, tbl_data in raw_tables.items():
            if not isinstance(key, str) or not key:
                raise StorageError("Invalid table key in database file")
            db.tables[key] = Table.from_dict(key, tbl_data)

        # Version 1 files predate indexes and load with an empty index catalog.
        raw_indexes = data.get("indexes", {})
        if not isinstance(raw_indexes, dict):
            raise StorageError("Invalid or missing 'indexes' mapping")
        for index_name, spec in raw_indexes.items():
            db.indexes[index_name] = _index_from_spec(db, index_name, spec)
        return db


def _index_from_spec(db: Database, name: Any, spec: Any) -> Index:
    """Validate a persisted index definition and rebuild its tree from table rows."""
    if not isinstance(name, str) or not INDEX_NAME_PATTERN.fullmatch(name):
        raise StorageError(f"Invalid index name {name!r} in database file")
    if not isinstance(spec, dict):
        raise StorageError(f"Invalid index definition for '{name}'")
    table_name = spec.get("table")
    column = spec.get("column")
    if not isinstance(table_name, str) or not table_name:
        raise StorageError(f"Invalid table reference for index '{name}'")
    if not isinstance(column, str) or not column:
        raise StorageError(f"Invalid column reference for index '{name}'")
    if table_name not in db.tables:
        raise StorageError(f"Index '{name}' references unknown table '{table_name}'")
    table = db.tables[table_name]
    if column not in table.column_names:
        raise StorageError(f"Index '{name}' references unknown column '{table_name}.{column}'")
    index = Index(name, table_name, column, table.rows)
    try:
        index.validate()
    except BTreeError as exc:
        raise StorageError(f"Index '{name}' is corrupted: {exc}") from exc
    return index


class JsonFileStorage:
    """WAL-backed atomic JSON file persistence backend for AayushSQL databases."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.wal = WalManager(self.path)
        self.pending_checkpoint: bool = False
        self.last_checkpoint_error: Exception | None = None

    def _load_snapshot(self) -> Database:
        if self.path.exists():
            if self.path.is_dir():
                raise StorageError(f"Database path '{self.path}' is a directory")
            try:
                raw_text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise StorageError(f"Failed to read database file '{self.path}': {exc}") from exc
            if not raw_text.strip():
                raise StorageError(f"Corrupted database file '{self.path}': file is empty")
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise StorageError(f"Corrupted database file '{self.path}': invalid JSON ({exc})") from exc
            return Database.from_dict(payload)
        return Database()

    def _cleanup_stale_tmp_files(self) -> None:
        parent = self.path.parent if str(self.path.parent) else Path(".")
        if not parent.exists() or not parent.is_dir():
            return
        prefix = f".{self.path.name}."
        try:
            for candidate in parent.iterdir():
                if (
                    candidate.is_file()
                    and candidate.name.startswith(prefix)
                    and candidate.name.endswith(".tmp")
                ):
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def load(self) -> Database:
        """Load the database snapshot and run WAL crash recovery if a WAL exists."""
        if self.path.exists() and self.path.is_dir():
            raise StorageError(f"Database path '{self.path}' is a directory")

        base_db = self._load_snapshot()
        self.pending_checkpoint = False
        self.last_checkpoint_error = None

        if not self.wal.wal_path.exists():
            self.wal.last_lsn = base_db.wal_lsn
            return base_db

        analysis = parse_wal(self.wal.wal_path, snapshot_wal_lsn=base_db.wal_lsn)
        applicable = [txn for txn in analysis.committed if txn.commit_lsn > base_db.wal_lsn]

        if applicable:
            if analysis.ignored_trailing:
                # Remove only the torn/corrupt tail before appending CHECKPOINT.
                # Appending behind it would turn a recoverable trailing fault into
                # mid-log corruption on the next restart. All valid committed
                # records stay in place and keep their LSNs.
                self.wal.repair_trailing(analysis.valid_bytes_end)
                _maybe_crash("after_tail_repair")
            latest = applicable[-1]
            recovered_db = Database.from_dict(latest.image)
            recovered_db.wal_lsn = latest.commit_lsn
            self.wal.last_lsn = max(latest.commit_lsn, analysis.max_lsn)
            self._checkpoint(recovered_db, latest.commit_lsn, txn_id=latest.txn)
            return recovered_db

        self.wal.last_lsn = max(base_db.wal_lsn, analysis.max_lsn)
        try:
            if self.wal.wal_path.exists() and self.wal.wal_path.stat().st_size > 0:
                self.wal.truncate()
            self._cleanup_stale_tmp_files()
        except OSError as exc:
            raise StorageError(f"Failed to clean up WAL '{self.wal.wal_path}': {exc}") from exc
        return base_db

    def _checkpoint(
        self,
        db: Database,
        commit_lsn: int,
        *,
        txn_id: str | None = None,
    ) -> None:
        """Atomically write snapshot with ``wal_lsn=commit_lsn``, append CHECKPOINT, and truncate WAL."""
        if self.path.exists() and self.path.is_dir():
            raise StorageError(f"Database path '{self.path}' is a directory")

        db.wal_lsn = commit_lsn
        payload = db.to_dict()
        parent = self.path.parent
        target_dir = parent if str(parent) else Path(".")
        tmp_path: Path | None = None
        try:
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target_dir,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                if os.environ.get(_CRASH_ENV_VAR) == "during_snapshot_write":
                    half = max(1, len(serialized) // 2)
                    tmp.write(serialized[:half])
                    tmp.flush()
                    os._exit(99)
                tmp.write(serialized)
                tmp.flush()
                os.fsync(tmp.fileno())
            _maybe_crash("after_snapshot_fsync_before_replace")
            os.replace(tmp_path, self.path)
            tmp_path = None
            fsync_dir(target_dir)
            _maybe_crash("after_snapshot_replace")

            self.wal.append_checkpoint(commit_lsn, txn_id=txn_id)
            self.wal.truncate()
            self._cleanup_stale_tmp_files()
            self.pending_checkpoint = False
            self.last_checkpoint_error = None
        except OSError as exc:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise StorageError(f"Failed to write database file '{self.path}': {exc}") from exc
        except Exception as exc:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"Failed to serialize database '{self.path}': {exc}") from exc

    def save(self, db: Database) -> None:
        """Durably commit ``db`` to the WAL, then checkpoint to the main database file."""
        if self.path.exists() and self.path.is_dir():
            raise StorageError(f"Database path '{self.path}' is a directory")
        try:
            payload = db.to_dict()
        except Exception as exc:
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"Failed to serialize database '{self.path}': {exc}") from exc

        commit_lsn, txn_id = self.wal.append_commit_transaction(payload)
        db.wal_lsn = commit_lsn

        try:
            self._checkpoint(db, commit_lsn, txn_id=txn_id)
        except Exception as exc:
            # The transaction is already durably committed in the WAL. Keep the WAL
            # intact so recovery can finish checkpointing later, and do not report
            # a misleading commit failure.
            self.pending_checkpoint = True
            self.last_checkpoint_error = exc

    def cleanup_if_clean(self) -> None:
        """Remove an empty 0-byte WAL when no checkpoint is pending."""
        if not self.pending_checkpoint:
            self.wal.remove_if_empty()

