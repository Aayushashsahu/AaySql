"""Offline backup and restore for AayushSQL databases.

Scope
-----
This is an **offline** facility: it requires exclusive ownership of the database
and therefore refuses to run while an AayushSQL server owns it. Live/online
backup (copying a database while it is being written) is deliberately not
implemented: a JSON snapshot plus a WAL cannot be copied consistently without a
read-mark mechanism, and pretending otherwise would risk a silently torn backup.

Backup guarantees
-----------------
* Exclusive access through the same OS-level lock file the server uses, so a
  running server and a backup can never overlap.
* Pending WAL is recovered and checkpointed *before* the snapshot is read, so the
  backup always contains the last durably committed state.
* The database bytes are copied verbatim; index definitions and rows live in that
  file, so they are preserved without reinterpretation.
* A manifest records the format version, engine version, creation metadata and a
  SHA-256 checksum of the database file.
* The backup is staged in a temporary directory and moved into place atomically.
  An existing backup destination is never replaced without ``force=True``.
* The source database and its WAL are only ever read.

Restore guarantees
------------------
* The manifest must be present, well formed, and describe a supported format.
* Checksums are verified before anything is written.
* The candidate database is opened through the normal storage/recovery path in a
  temporary location, so a backup that would not load is rejected.
* A database owned by a running server is never replaced.
* An existing target database requires ``force=True``. When it is replaced, it is
  first moved aside and kept until the restored database has been opened
  successfully; the caller is told where the previous copy lives.
* A target with a non-empty WAL is refused: that WAL may hold committed
  transactions that the restore would silently disown.
* Replacement uses ``os.replace`` on a staged file in the target directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .errors import AayushSQLError
from .server import LOCK_SUFFIX, ServerLock, ServerConfigError
from .storage import FORMAT_VERSION
from .wal import fsync_dir

BACKUP_FORMAT = "aaysql-backup"
BACKUP_FORMAT_VERSION = 1
SUPPORTED_BACKUP_FORMATS = (1,)
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.aaydb"


class BackupError(AayushSQLError):
    """A backup or restore could not be completed safely."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lock_path_for(db_path: Path) -> Path:
    return Path(f"{db_path}{LOCK_SUFFIX}")


def _acquire(db_path: Path) -> ServerLock:
    lock = ServerLock(_lock_path_for(db_path))
    try:
        lock.acquire()
    except ServerConfigError as exc:
        raise BackupError(
            f"database '{db_path}' is in use (a server owns it); stop the server first"
        ) from exc
    return lock


def _read_database_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupError(f"cannot read database file '{path}': {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackupError(f"database file '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BackupError(f"database file '{path}' is not a database snapshot")
    return payload


def _summarize(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], int]:
    """Return (tables, index names, wal_lsn) from a database payload."""
    tables: list[dict[str, Any]] = []
    raw_tables = payload.get("tables")
    if isinstance(raw_tables, dict):
        for name, table in sorted(raw_tables.items()):
            rows = table.get("rows") if isinstance(table, dict) else None
            tables.append({"name": name, "rows": len(rows) if isinstance(rows, list) else 0})
    raw_indexes = payload.get("indexes")
    indexes = sorted(raw_indexes) if isinstance(raw_indexes, dict) else []
    wal_lsn = payload.get("wal_lsn", 0)
    return tables, indexes, wal_lsn if isinstance(wal_lsn, int) else 0


def _fsync_file(path: Path) -> None:
    """Flush a file's contents to stable storage.

    Opened for read/write: Windows rejects ``os.fsync`` on a read-only handle
    (EBADF), and both call sites own the file being flushed.
    """
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _recover_pending_wal(db_path: Path) -> None:
    """Load and close the database through the normal path to apply any WAL."""
    from .engine import Engine  # local import keeps module import cheap

    try:
        engine = Engine(db_path)
    except AayushSQLError as exc:
        raise BackupError(f"database '{db_path}' cannot be opened for backup: {exc}") from exc
    try:
        if engine.needs_recovery or engine.wal_poisoned:
            raise BackupError(
                f"database '{db_path}' requires recovery before it can be backed up"
            )
    finally:
        engine.close()


def backup_database(
    db_path: str | Path,
    destination: str | Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create an offline backup directory for ``db_path`` at ``destination``.

    Returns the manifest that was written. Raises :class:`BackupError` when the
    source is missing, owned by a server, unrecoverable, or the destination
    already exists without ``force``.
    """
    source = Path(db_path)
    dest = Path(destination)
    if not source.exists():
        raise BackupError(f"database file '{source}' does not exist")
    if source.is_dir():
        raise BackupError(f"database path '{source}' is a directory")
    if dest == source:
        raise BackupError("backup destination must differ from the database path")
    if dest.exists() and not force:
        raise BackupError(
            f"backup destination '{dest}' already exists; pass force=True to replace it"
        )
    if dest.exists() and not dest.is_dir():
        raise BackupError(f"backup destination '{dest}' exists and is not a directory")

    lock = _acquire(source)
    staging: Path | None = None
    previous: Path | None = None
    try:
        _recover_pending_wal(source)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise BackupError(f"cannot read database file '{source}': {exc}") from exc
        if not data.strip():
            raise BackupError(f"database file '{source}' is empty")

        payload = _read_database_payload(source)
        tables, indexes, wal_lsn = _summarize(payload)
        manifest: dict[str, Any] = {
            "backup_format": BACKUP_FORMAT,
            "backup_format_version": BACKUP_FORMAT_VERSION,
            "engine_version": __version__,
            "created_at": _utc_now(),
            "source_database": source.name,
            "source_format_version": payload.get("format_version"),
            "database_format_version": payload.get("format_version"),
            "database_file": DATABASE_NAME,
            "database_size": len(data),
            "database_sha256": _sha256_bytes(data),
            "wal_lsn": wal_lsn,
            "tables": tables,
            "indexes": indexes,
            "consistency": "offline snapshot after WAL recovery",
        }

        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{dest.name}.tmp-", dir=str(dest.parent))
        )
        db_copy = staging / DATABASE_NAME
        db_copy.write_bytes(data)
        _fsync_file(db_copy)
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _fsync_file(manifest_path)
        fsync_dir(staging)

        if dest.exists():
            previous = Path(f"{dest}.old-{uuid.uuid4().hex}")
            os.replace(dest, previous)
        try:
            os.replace(staging, dest)
            staging = None
        except OSError as exc:
            if previous is not None and previous.exists() and not dest.exists():
                os.replace(previous, dest)
                previous = None
            raise BackupError(f"cannot install backup at '{dest}': {exc}") from exc
        fsync_dir(dest.parent)
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
        return manifest
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        lock.release()


def read_manifest(backup_dir: str | Path) -> dict[str, Any]:
    """Read and validate a backup manifest without touching the database."""
    root = Path(backup_dir)
    manifest_path = root / MANIFEST_NAME
    if not root.is_dir():
        raise BackupError(f"backup directory '{root}' does not exist")
    if not manifest_path.is_file():
        raise BackupError(f"backup at '{root}' has no {MANIFEST_NAME}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid backup manifest '{manifest_path}': {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest must be a JSON object")
    if manifest.get("backup_format") != BACKUP_FORMAT:
        raise BackupError(
            f"not an AayushSQL backup (backup_format={manifest.get('backup_format')!r})"
        )
    version = manifest.get("backup_format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise BackupError("backup manifest has no valid backup_format_version")
    if version not in SUPPORTED_BACKUP_FORMATS:
        raise BackupError(f"unsupported backup format version {version}")
    db_version = manifest.get("database_format_version")
    if isinstance(db_version, bool) or not isinstance(db_version, int):
        raise BackupError("backup manifest has no valid database_format_version")
    if db_version > FORMAT_VERSION:
        raise BackupError(
            f"backup was written by a newer database format ({db_version} > {FORMAT_VERSION})"
        )
    name = manifest.get("database_file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise BackupError("backup manifest has an invalid database_file name")
    digest = manifest.get("database_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise BackupError("backup manifest has no valid database_sha256")
    size = manifest.get("database_size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise BackupError("backup manifest has no valid database_size")
    return manifest


def verify_backup(backup_dir: str | Path) -> dict[str, Any]:
    """Validate a backup's manifest, checksum, and loadability.

    Does not modify the backup: the database is validated on a temporary copy.
    """
    root = Path(backup_dir)
    manifest = read_manifest(root)
    db_file = root / manifest["database_file"]
    if not db_file.is_file():
        raise BackupError(f"backup is missing '{manifest['database_file']}'")
    try:
        data = db_file.read_bytes()
    except OSError as exc:
        raise BackupError(f"cannot read backup database '{db_file}': {exc}") from exc
    if len(data) != manifest["database_size"]:
        raise BackupError(
            f"backup size mismatch: manifest says {manifest['database_size']} bytes, "
            f"file has {len(data)}"
        )
    actual = _sha256_bytes(data)
    if actual != manifest["database_sha256"]:
        raise BackupError(
            f"backup checksum mismatch: manifest says {manifest['database_sha256']}, "
            f"file hashes to {actual}"
        )
    with tempfile.TemporaryDirectory(prefix="aaysql-backup-verify-") as tmp:
        candidate = Path(tmp) / "verify.aaydb"
        candidate.write_bytes(data)
        _recover_pending_wal(candidate)
    return manifest


def restore_database(
    backup_dir: str | Path,
    target_path: str | Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Restore a backup into ``target_path``.

    Returns a report with the manifest, the restored path, and the location of the
    preserved previous database (if one was replaced).
    """
    root = Path(backup_dir)
    target = Path(target_path)
    manifest = verify_backup(root)
    source_db = root / manifest["database_file"]

    if target.exists() and target.is_dir():
        raise BackupError(f"restore target '{target}' is a directory")
    if target.exists() and not force:
        raise BackupError(
            f"restore target '{target}' already exists; pass force=True to replace it"
        )

    lock = _acquire(target)
    staged: Path | None = None
    previous: Path | None = None
    try:
        # A non-empty WAL belongs to the *current* target database and may hold
        # committed transactions. Restoring over it would discard them silently.
        wal_path = Path(f"{target}.wal")
        if wal_path.exists() and wal_path.is_file() and wal_path.stat().st_size > 0:
            raise BackupError(
                f"restore target '{target}' has unrecovered WAL data; start the "
                "database once so recovery applies it, then restore"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.restore-", suffix=".tmp", dir=str(target.parent)
        )
        os.close(fd)
        staged = Path(raw_path)
        try:
            staged.write_bytes(source_db.read_bytes())
            _fsync_file(staged)
        except OSError as exc:
            raise BackupError(f"cannot stage restored database: {exc}") from exc

        # Validate the candidate through the exact loading path a user would use.
        try:
            _recover_pending_wal(staged)
        except BackupError as exc:
            raise BackupError(f"backup cannot be restored: {exc}") from exc

        if target.exists():
            previous = Path(f"{target}.pre-restore-{uuid.uuid4().hex}")
            os.replace(target, previous)
        try:
            os.replace(staged, target)
            staged = None
        except OSError as exc:
            if previous is not None and previous.exists():
                os.replace(previous, target)
                previous = None
            raise BackupError(f"cannot install restored database at '{target}': {exc}") from exc
        fsync_dir(target.parent)

        # Remove only the empty WAL that belonged to the replaced database; there
        # is nothing to recover from a 0-byte file.
        if wal_path.exists() and wal_path.is_file() and wal_path.stat().st_size == 0:
            try:
                wal_path.unlink()
            except OSError as exc:
                raise BackupError(
                    f"restored '{target}' but could not remove the empty WAL: {exc}"
                ) from exc

        try:
            _recover_pending_wal(target)
        except BackupError:
            if previous is not None and previous.exists():
                os.replace(previous, target)
                previous = None
            raise
        return {
            "restored": str(target),
            "backup": str(root),
            "manifest": manifest,
            "previous_database": str(previous) if previous is not None else None,
        }
    finally:
        if staged is not None and staged.exists():
            try:
                staged.unlink()
            except OSError:
                pass
        lock.release()


# --- CLI -------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aaysql-backup",
        description="Offline backup, restore, and verification for AayushSQL databases.",
    )
    parser.add_argument("--version", action="version", version=f"aaysql-backup {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="copy a database into a backup directory")
    backup.add_argument("database", help="path to the .aaydb file")
    backup.add_argument("destination", help="backup directory to create")
    backup.add_argument("--force", action="store_true", help="replace an existing backup")

    restore = sub.add_parser("restore", help="restore a backup directory to a database path")
    restore.add_argument("backup", help="backup directory")
    restore.add_argument("database", help="target .aaydb path")
    restore.add_argument("--force", action="store_true", help="replace an existing database")

    verify = sub.add_parser("verify", help="validate a backup without restoring it")
    verify.add_argument("backup", help="backup directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "backup":
            manifest = backup_database(args.database, args.destination, force=args.force)
            print(
                f"backup written to {args.destination} "
                f"(tables={len(manifest['tables'])}, indexes={len(manifest['indexes'])}, "
                f"sha256={manifest['database_sha256']})"
            )
            return 0
        if args.command == "restore":
            report = restore_database(args.backup, args.database, force=args.force)
            print(f"restored {args.database} from {args.backup}")
            if report["previous_database"]:
                print(f"previous database preserved at {report['previous_database']}")
            return 0
        manifest = verify_backup(args.backup)
        print(
            f"backup OK: format {manifest['database_format_version']}, "
            f"created {manifest['created_at']}, sha256={manifest['database_sha256']}"
        )
        return 0
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except AayushSQLError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
