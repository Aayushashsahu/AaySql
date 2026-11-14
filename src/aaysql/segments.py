"""Format v4 root manifest and immutable checkpoint segments.

The database path holds the root manifest; it is the single authoritative
pointer to one complete database generation. Segments are content-addressed
immutable files in ``<db>.seg/``, so an unchanged table is reused by
construction and a checkpoint only writes the tables that changed.

This module deliberately stores *logical* data only (columns, rows, index
definitions). B+ tree structure, statistics and other runtime caches are rebuilt
from rows when a database is loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import StorageError
from .wal import _maybe_crash, fsync_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .storage import Database, Table

MANIFEST_FORMAT = "aaysql"
MANIFEST_FORMAT_VERSION = 4
SEGMENT_FORMAT = "aaysql-segment"
SEGMENT_SUFFIX = ".seg"
SEGMENT_DIR_SUFFIX = ".seg"
SEGMENT_NAME_PATTERN = re.compile(r"^seg_[0-9a-f]{24}\.seg$")

MANIFEST_KEYS = frozenset(
    {
        "format",
        "format_version",
        "engine_version",
        "db_id",
        "generation",
        "checkpoint_lsn",
        "tables",
        "indexes",
    }
)
SEGMENT_KEYS = frozenset({"format", "format_version", "table", "columns", "rows"})
TABLE_REF_KEYS = frozenset({"segment", "generation", "length", "sha256"})
DB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def segment_dir(db_path: str | Path) -> Path:
    """Directory holding the immutable segments for ``db_path``."""
    return Path(f"{Path(db_path)}{SEGMENT_DIR_SUFFIX}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def segment_name_for(digest: str) -> str:
    """Content-addressed file name for a segment payload digest."""
    return f"seg_{digest[:24]}{SEGMENT_SUFFIX}"


def canonical_json_bytes(payload: Any) -> bytes:
    """Deterministic serialization used for segment content and checksums."""
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"Failed to serialize database value: {exc}") from exc
    return text.encode("utf-8")


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Human-inspectable, deterministic manifest serialization."""
    try:
        text = json.dumps(
            manifest, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"Failed to serialize root manifest: {exc}") from exc
    return (text + "\n").encode("utf-8")


def validate_segment_name(name: Any) -> str:
    """Reject anything that is not a plain content-addressed segment file name.

    The manifest is untrusted input, so table names never become paths and a
    corrupt or malicious manifest cannot address files outside the segment
    directory.
    """
    if not isinstance(name, str) or not SEGMENT_NAME_PATTERN.fullmatch(name):
        raise StorageError(f"Invalid segment file name {name!r} in root manifest")
    return name


def segment_path(db_path: Path, name: str) -> Path:
    """Resolve ``name`` inside the segment directory, refusing escapes."""
    directory = segment_dir(db_path)
    candidate = directory / validate_segment_name(name)
    try:
        parent = candidate.parent.resolve()
        expected = directory.resolve()
    except OSError as exc:  # pragma: no cover - unreadable parent
        raise StorageError(
            f"Cannot resolve segment directory '{directory}': {exc}"
        ) from exc
    if parent != expected:
        raise StorageError(f"Segment '{name}' resolves outside '{directory}'")
    return candidate


def _column_payload(column: Any) -> dict[str, Any]:
    return {
        "name": column.name,
        "type_name": column.type_name,
        "primary_key": column.primary_key,
        "not_null": column.not_null,
        "unique": column.unique,
        "default": column.default,
        "has_default": column.has_default,
    }


def encode_segment(table: Table) -> dict[str, Any]:
    """Build the logical payload for one table (rows stored positionally)."""
    from .storage import serialize_row_for_storage

    column_names = [column.name for column in table.columns]
    rows: list[list[Any]] = []
    for row in table.rows:
        try:
            serialized_row = serialize_row_for_storage(table.columns, row)
            rows.append([serialized_row[name] for name in column_names])
        except KeyError as exc:  # pragma: no cover - defensive
            raise StorageError(
                f"Row in table '{table.name}' is missing column {exc.args[0]!r}"
            ) from exc
    return {
        "format": SEGMENT_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "table": table.name,
        "columns": [_column_payload(column) for column in table.columns],
        "rows": rows,
    }


def write_segment(db_path: Path, table: Table, *, generation: int) -> dict[str, Any]:
    """Write one immutable segment and return its manifest reference.

    The file name is derived from the payload digest. If a file with that name
    already exists its bytes must match, otherwise the directory contains a
    collision or corruption and the checkpoint fails rather than overwriting
    data a live manifest might reference.
    """
    data = canonical_json_bytes(encode_segment(table)) + b"\n"
    digest = sha256_hex(data)
    name = segment_name_for(digest)
    directory = segment_dir(db_path)
    target = directory / name

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(
            f"Failed to create segment directory '{directory}': {exc}"
        ) from exc

    tmp_path: Path | None = None
    try:
        if target.exists():
            if not target.is_file():
                raise StorageError(f"Segment path '{target}' exists and is not a file")
            if (
                target.stat().st_size == len(data)
                and sha256_hex(target.read_bytes()) == digest
            ):
                return {
                    "segment": name,
                    "generation": generation,
                    "length": len(data),
                    "sha256": digest,
                }
            raise StorageError(
                f"Segment '{name}' already exists with different content; "
                "the segment directory is corrupt"
            )
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(directory),
            prefix=f".{name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _maybe_crash("after_segment_write_before_replace")
        os.replace(tmp_path, target)
        tmp_path = None
        fsync_dir(directory)
        _maybe_crash("after_segment_fsync")
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"Failed to write segment '{target}': {exc}") from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return {
        "segment": name,
        "generation": generation,
        "length": len(data),
        "sha256": digest,
    }


def _validate_ref(ref: Any, table_name: str) -> tuple[str, int, str, int]:
    """Validate the manifest reference for one table and unpack its fields."""
    if not isinstance(ref, dict) or set(ref.keys()) != TABLE_REF_KEYS:
        raise StorageError(f"Invalid segment reference for table '{table_name}'")
    name = validate_segment_name(ref["segment"])
    length = ref["length"]
    digest = ref["sha256"]
    generation = ref["generation"]
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise StorageError(f"Invalid segment length for table '{table_name}'")
    if not isinstance(digest, str) or len(digest) != 64:
        raise StorageError(f"Invalid segment checksum for table '{table_name}'")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise StorageError(f"Invalid segment generation for table '{table_name}'")
    return name, length, digest, generation


def read_segment(db_path: Path, ref: dict[str, Any], *, table_name: str) -> Table:
    """Load and fully validate one segment referenced by the manifest."""
    from .storage import Table  # local import avoids a module cycle

    name, length, digest, _generation = _validate_ref(ref, table_name)
    path = segment_path(db_path, name)
    if not path.is_file():
        raise StorageError(
            f"Missing segment '{name}' for table '{table_name}' in "
            f"'{segment_dir(db_path)}'"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StorageError(f"Failed to read segment '{path}': {exc}") from exc
    if len(data) != length:
        raise StorageError(
            f"Segment '{name}' is {len(data)} bytes, manifest says {length}"
        )
    actual = sha256_hex(data)
    if actual != digest:
        raise StorageError(
            f"Segment '{name}' checksum mismatch: manifest says {digest}, "
            f"file hashes to {actual}"
        )
    if name != segment_name_for(digest):
        raise StorageError(f"Segment '{name}' does not match its manifest checksum")
    return _decode_segment(name, table_name, data, Table)


def _decode_segment(name: str, table_name: str, data: bytes, table_cls: Any) -> Table:
    """Parse a verified segment payload into a table."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"Corrupted segment '{name}': invalid JSON ({exc})") from exc
    if not isinstance(payload, dict) or set(payload.keys()) != SEGMENT_KEYS:
        raise StorageError(f"Corrupted segment '{name}': unexpected field set")
    if payload.get("format") != SEGMENT_FORMAT:
        raise StorageError(f"Corrupted segment '{name}': unknown format")
    if payload.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise StorageError(
            f"Corrupted segment '{name}': unsupported segment format version "
            f"{payload.get('format_version')!r}"
        )
    if payload.get("table") != table_name:
        raise StorageError(
            f"Segment '{name}' holds table {payload.get('table')!r}, "
            f"manifest says '{table_name}'"
        )
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not columns:
        raise StorageError(f"Corrupted segment '{name}': missing column list")
    if not isinstance(rows, list):
        raise StorageError(f"Corrupted segment '{name}': missing row list")
    names: list[str] = []
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("name"), str):
            raise StorageError(f"Corrupted segment '{name}': invalid column metadata")
        names.append(column["name"])
    materialized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(names):
            raise StorageError(
                f"Corrupted segment '{name}': row {index} does not match the "
                "column count"
            )
        materialized.append(dict(zip(names, row)))
    source = {"name": table_name, "columns": columns, "rows": materialized}
    try:
        return table_cls.from_dict(table_name, source)
    except StorageError:
        raise
    except Exception as exc:  # pragma: no cover - defensive conversion
        raise StorageError(f"Corrupted segment '{name}': {exc}") from exc


def build_manifest(
    *,
    engine_version: str,
    db_id: str,
    generation: int,
    checkpoint_lsn: int,
    tables: dict[str, dict[str, Any]],
    indexes: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Assemble a root manifest from per-table segment references."""
    if not DB_ID_PATTERN.fullmatch(db_id):
        raise StorageError(f"Invalid database identity {db_id!r}")
    return {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "engine_version": engine_version,
        "db_id": db_id,
        "generation": generation,
        "checkpoint_lsn": checkpoint_lsn,
        "tables": {
            name: {
                "segment": ref["segment"],
                "generation": ref["generation"],
                "length": ref["length"],
                "sha256": ref["sha256"],
            }
            for name, ref in sorted(tables.items())
        },
        "indexes": {
            name: {"table": spec["table"], "column": spec["column"]}
            for name, spec in sorted(indexes.items())
        },
    }


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError(f"Invalid '{field}' in root manifest")
    return value


def validate_manifest(payload: Any) -> dict[str, Any]:
    """Validate a parsed root manifest and return a normalized copy."""
    if not isinstance(payload, dict):
        raise StorageError("Root manifest must be a JSON object")
    if set(payload.keys()) != MANIFEST_KEYS:
        missing = sorted(MANIFEST_KEYS - set(payload.keys()))
        extra = sorted(set(payload.keys()) - MANIFEST_KEYS)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise StorageError(f"Invalid root manifest fields: {'; '.join(detail)}")
    if payload["format"] != MANIFEST_FORMAT:
        raise StorageError(f"Unsupported database format: {payload['format']!r}")
    if payload["format_version"] != MANIFEST_FORMAT_VERSION:
        raise StorageError(
            f"Unsupported database format version: {payload['format_version']!r} "
            f"(this build reads version {MANIFEST_FORMAT_VERSION})"
        )
    engine_version = payload["engine_version"]
    if not isinstance(engine_version, str) or not engine_version:
        raise StorageError("Invalid 'engine_version' in root manifest")
    db_id = payload["db_id"]
    if not isinstance(db_id, str) or not DB_ID_PATTERN.fullmatch(db_id):
        raise StorageError("Invalid 'db_id' in root manifest")
    generation = _non_negative_int(payload["generation"], "generation")
    checkpoint_lsn = _non_negative_int(payload["checkpoint_lsn"], "checkpoint_lsn")
    raw_tables = payload["tables"]
    if not isinstance(raw_tables, dict):
        raise StorageError("Invalid 'tables' mapping in root manifest")
    tables: dict[str, dict[str, Any]] = {}
    for name, ref in raw_tables.items():
        if not isinstance(name, str) or not name:
            raise StorageError("Invalid table name in root manifest")
        seg_name, length, digest, seg_generation = _validate_ref(ref, name)
        tables[name] = {
            "segment": seg_name,
            "generation": seg_generation,
            "length": length,
            "sha256": digest,
        }
    raw_indexes = payload["indexes"]
    if not isinstance(raw_indexes, dict):
        raise StorageError("Invalid 'indexes' mapping in root manifest")
    indexes: dict[str, dict[str, str]] = {}
    for name, spec in raw_indexes.items():
        if not isinstance(name, str) or not name:
            raise StorageError("Invalid index name in root manifest")
        if not isinstance(spec, dict) or set(spec.keys()) != {"table", "column"}:
            raise StorageError(f"Invalid index definition '{name}' in root manifest")
        table_name = spec["table"]
        column = spec["column"]
        if not isinstance(table_name, str) or table_name not in tables:
            raise StorageError(
                f"Index '{name}' references unknown table {table_name!r}"
            )
        if not isinstance(column, str) or not column:
            raise StorageError(f"Index '{name}' has an invalid column name")
        indexes[name] = {"table": table_name, "column": column}
    return {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "engine_version": engine_version,
        "db_id": db_id,
        "generation": generation,
        "checkpoint_lsn": checkpoint_lsn,
        "tables": tables,
        "indexes": indexes,
    }


def read_manifest(db_path: Path) -> dict[str, Any]:
    """Read and validate the root manifest stored at ``db_path``."""
    try:
        data = db_path.read_bytes()
    except FileNotFoundError as exc:
        raise StorageError(f"Database file '{db_path}' does not exist") from exc
    except OSError as exc:
        raise StorageError(f"Failed to read database file '{db_path}': {exc}") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError(
            f"Corrupted root manifest '{db_path}': invalid JSON ({exc})"
        ) from exc
    return validate_manifest(payload)


def publish_manifest(db_path: Path, manifest: dict[str, Any]) -> None:
    """Atomically replace the root manifest with ``manifest``.

    The new bytes are flushed and fsynced before the rename, and the parent
    directory is fsynced afterwards where the platform supports it, so a crash
    can leave either the previous complete manifest or the new one, never a
    partial mixture. A failure here is reported to the caller; the WAL has
    already committed, so callers must not describe it as a rollback.
    """
    validated = validate_manifest(manifest)
    data = manifest_bytes(validated)
    parent = db_path.parent if str(db_path.parent) else Path(".")
    if not parent.is_dir():
        raise StorageError(f"Database directory '{parent}' does not exist")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(parent),
            prefix=f".{db_path.name}.manifest.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _maybe_crash("after_manifest_write_before_replace")
        os.replace(tmp_path, db_path)
        tmp_path = None
        fsync_dir(parent)
        _maybe_crash("after_manifest_replace")
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(
            f"Failed to publish root manifest '{db_path}': {exc}"
        ) from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_database(db_path: Path, manifest: dict[str, Any]) -> Database:
    """Materialize a database generation from its manifest and segments."""
    from .errors import ExecutionError
    from .storage import Database

    db = Database()
    db.wal_lsn = manifest["checkpoint_lsn"]
    for name, ref in manifest["tables"].items():
        db.tables[name] = read_segment(db_path, ref, table_name=name)
    for name, spec in manifest["indexes"].items():
        try:
            db.create_index(name, spec["table"], spec["column"])
        except ExecutionError as exc:
            raise StorageError(
                f"Corrupted index catalog in root manifest: {exc}"
            ) from exc
    return db


def collect_garbage(db_path: Path, manifest: dict[str, Any]) -> list[str]:
    """Delete segment files that the given manifest does not reference.

    Garbage collection is best effort: a file that cannot be removed is left in
    place and simply collected by a later checkpoint. Nothing here can make a
    committed generation unreadable because only unreferenced files are removed.
    """
    directory = segment_dir(db_path)
    if not directory.is_dir():
        return []
    referenced = {ref["segment"] for ref in manifest["tables"].values()}
    removed: list[str] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name in referenced:
            continue
        try:
            if not entry.is_file() or entry.is_symlink():
                continue
            entry.unlink()
            removed.append(entry.name)
        except OSError:
            continue
    return sorted(removed)
