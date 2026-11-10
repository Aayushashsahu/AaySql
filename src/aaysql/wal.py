"""Write-Ahead Log (WAL) protocol, integrity verification, and durability helpers."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AmbiguousCommitError, StorageError

WAL_RECORD_TYPES = frozenset({"begin", "write", "commit", "checkpoint"})
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "begin": frozenset({"lsn", "txn", "type", "checksum"}),
    "write": frozenset({"lsn", "txn", "type", "image", "checksum"}),
    "commit": frozenset({"lsn", "txn", "type", "write_lsn", "checksum"}),
    "checkpoint": frozenset({"lsn", "txn", "type", "commit_lsn", "checksum"}),
}
_CRASH_ENV_VAR = "_AAYSQL_TEST_CRASH_POINT"
_DIR_FSYNC_FAULT_ENV_VAR = "_AAYSQL_TEST_DIR_FSYNC_ERROR"
_WAL_WRITE_LIMIT_ENV_VAR = "_AAYSQL_TEST_WAL_WRITE_LIMIT"

# Directory-entry fsync is conceptually supported on POSIX only. Windows cannot
# open a directory through the CRT, so directory durability there relies on NTFS
# metadata behaviour (documented in WAL.md).
_DIR_FSYNC_UNSUPPORTED_ERRNOS: frozenset[int] = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


def _maybe_crash(point: str) -> None:
    """Internal crash-testing hook. Terminates immediately via os._exit without cleanup."""
    if os.environ.get(_CRASH_ENV_VAR) == point:
        os._exit(99)


def _reject_non_finite(token: str) -> Any:
    raise ValueError(f"Non-finite JSON constant '{token}' is not allowed in WAL records")


def _directory_fsync_supported() -> bool:
    """Return True when the platform supports fsync on a directory handle.

    Windows (``os.name == "nt"``) does not support opening directories through
    ``os.open()``; calling it there raises ``PermissionError`` regardless of
    filesystem health, so support is decided by platform rather than by errno.
    """
    return os.name != "nt"


def fsync_dir(dir_path: Path) -> None:
    """Fsync a directory entry where the platform supports it.

    Raises :class:`StorageError` for genuine filesystem failures (``EIO``,
    ``ENOSPC``, permission errors, ...) so durability is never silently claimed
    after a real I/O error. Filesystems that explicitly report that directory
    fsync is unsupported (``EINVAL`` / ``ENOTSUP`` / ``EOPNOTSUPP``) fall back
    to the documented platform behaviour. On Windows the directory fsync is
    skipped deliberately: the CRT cannot open directories, so directory-entry
    durability depends on NTFS metadata behaviour.
    """
    target = dir_path if str(dir_path) else Path(".")

    # Test-only fault injection for subprocess durability scenarios. It is read
    # from the environment and never reachable from the SQL surface.
    injected = os.environ.get(_DIR_FSYNC_FAULT_ENV_VAR)
    if injected:
        code = getattr(errno, injected, errno.EIO)
        raise StorageError(
            f"Injected directory fsync failure ({injected}) for '{target}'"
        ) from OSError(code, f"injected {injected}")

    if not _directory_fsync_supported():
        return

    flags = os.O_RDONLY
    try:
        fd = os.open(str(target), flags)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED_ERRNOS:
            return
        raise StorageError(
            f"Failed to open directory '{target}' for fsync: {exc}"
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED_ERRNOS:
            return
        raise StorageError(f"Failed to fsync directory '{target}': {exc}") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def compute_checksum(record: dict[str, Any]) -> str:
    """Compute canonical SHA-256 checksum over a WAL record excluding 'checksum'."""
    canonical_payload = {k: v for k, v in record.items() if k != "checksum"}
    canonical_bytes = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def build_record(lsn: int, txn: str, rec_type: str, **extra: Any) -> dict[str, Any]:
    """Build a WAL record dictionary with its deterministic SHA-256 checksum."""
    record: dict[str, Any] = {
        "lsn": lsn,
        "txn": txn,
        "type": rec_type,
        **extra,
    }
    record["checksum"] = compute_checksum(record)
    return record


def serialize_record(record: dict[str, Any]) -> bytes:
    """Serialize a WAL record as canonical newline-terminated UTF-8 JSON."""
    line = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return (line + "\n").encode("utf-8")


def verify_record_checksum(record: dict[str, Any]) -> bool:
    """Verify the SHA-256 checksum of a parsed WAL record."""
    checksum = record.get("checksum")
    if not isinstance(checksum, str) or len(checksum) != 64:
        return False
    expected = compute_checksum(record)
    return hmac.compare_digest(expected, checksum)


@dataclass
class _TxnEntry:
    txn: str
    begin_lsn: int
    write_lsn: int | None = None
    commit_lsn: int | None = None
    image: dict[str, Any] | None = None
    state: str = "begun"  # begun -> written -> committed


@dataclass(frozen=True)
class CommittedTxn:
    txn: str
    begin_lsn: int
    write_lsn: int
    commit_lsn: int
    image: dict[str, Any]


@dataclass
class WalAnalysis:
    records: list[dict[str, Any]]
    committed: list[CommittedTxn]
    max_lsn: int
    ignored_trailing: bool
    discarded_uncommitted: int
    # Exact byte offset immediately after the last fully valid, newline-terminated
    # record. Everything from this offset onward is a torn/corrupt tail.
    valid_bytes_end: int = 0


def parse_wal(wal_path: Path, snapshot_wal_lsn: int = 0) -> WalAnalysis:
    """Read, validate, and analyze WAL records from disk.

    Stops at the first invalid or incomplete record. Trailing incomplete or
    checksum-corrupt records (at the very end of the WAL) are safely ignored as
    crash-torn writes. Any invalid record *before* subsequent data raises
    StorageError so corruption is never silently skipped.

    ``WalAnalysis.valid_bytes_end`` reports the exact byte boundary after the
    last validated record so callers can repair only the invalid tail.
    """
    if wal_path.is_dir():
        raise StorageError(f"WAL path '{wal_path}' is a directory")
    try:
        raw_bytes = wal_path.read_bytes()
    except OSError as exc:
        raise StorageError(f"Failed to read WAL file '{wal_path}': {exc}") from exc

    if not raw_bytes:
        return WalAnalysis(
            records=[],
            committed=[],
            max_lsn=0,
            ignored_trailing=False,
            discarded_uncommitted=0,
            valid_bytes_end=0,
        )

    # Split on the record delimiter only (bytes.splitlines() would also split on
    # lone \r, which is not a record boundary here). The trailing element is the
    # last record, possibly unterminated after a torn write.
    terminated = raw_bytes.endswith(b"\n")
    segments = raw_bytes.split(b"\n")
    if terminated and segments and segments[-1] == b"":
        segments = segments[:-1]

    prev_lsn = 0
    seen_txns: dict[str, _TxnEntry] = {}
    records: list[dict[str, Any]] = []
    committed: list[CommittedTxn] = []
    ignored_trailing = False
    valid_bytes_end = 0
    offset = 0

    for idx, raw_segment in enumerate(segments):
        is_last = idx == len(segments) - 1
        has_newline = not (is_last and not terminated)
        record_end = offset + len(raw_segment) + (1 if has_newline else 0)

        def handle_invalid(reason: str, *, _is_last: bool = is_last, _idx: int = idx) -> None:
            nonlocal ignored_trailing
            if _is_last:
                ignored_trailing = True
                return
            raise StorageError(
                f"Corrupted WAL record at line {_idx + 1} in '{wal_path}': {reason}"
            )

        try:
            if not has_newline:
                handle_invalid("incomplete record missing trailing newline")
                break

            try:
                text_line = raw_segment.decode("utf-8", errors="strict")
                rec = json.loads(text_line, parse_constant=_reject_non_finite)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                handle_invalid(f"malformed JSON ({exc})")
                break

            if not isinstance(rec, dict):
                handle_invalid("WAL record must be a JSON object")
                break

            if not verify_record_checksum(rec):
                handle_invalid("checksum verification failed")
                break

            rec_type = rec.get("type")
            if not isinstance(rec_type, str) or rec_type not in WAL_RECORD_TYPES:
                raise StorageError(
                    f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                    f"unknown record type {rec_type!r}"
                )

            if set(rec.keys()) != _ALLOWED_KEYS[rec_type]:
                raise StorageError(
                    f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                    f"unexpected or missing fields for '{rec_type}' record"
                )

            lsn = rec.get("lsn")
            if isinstance(lsn, bool) or not isinstance(lsn, int) or lsn <= 0:
                raise StorageError(
                    f"Corrupted WAL record at line {idx + 1} in '{wal_path}': invalid LSN"
                )
            if lsn <= prev_lsn:
                raise StorageError(
                    f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                    f"non-monotonic LSN {lsn} after {prev_lsn}"
                )

            txn = rec.get("txn")
            if not isinstance(txn, str) or not txn:
                raise StorageError(
                    f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                    "invalid transaction identifier"
                )

            if rec_type == "begin":
                if txn in seen_txns:
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        f"duplicate BEGIN for transaction '{txn}'"
                    )
                seen_txns[txn] = _TxnEntry(txn=txn, begin_lsn=lsn)
            elif rec_type == "write":
                entry = seen_txns.get(txn)
                if entry is None or entry.state != "begun":
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        f"WRITE without matching active BEGIN for transaction '{txn}'"
                    )
                image = rec.get("image")
                if not isinstance(image, dict):
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        "WRITE record missing serialized database image object"
                    )
                entry.write_lsn = lsn
                entry.image = image
                entry.state = "written"
            elif rec_type == "commit":
                entry = seen_txns.get(txn)
                if (
                    entry is None
                    or entry.state != "written"
                    or entry.write_lsn is None
                    or entry.image is None
                ):
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        f"COMMIT without matching WRITE for transaction '{txn}'"
                    )
                write_lsn = rec.get("write_lsn")
                if (
                    isinstance(write_lsn, bool)
                    or not isinstance(write_lsn, int)
                    or write_lsn != entry.write_lsn
                ):
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        f"COMMIT write_lsn {write_lsn!r} does not match WRITE LSN {entry.write_lsn}"
                    )
                entry.commit_lsn = lsn
                entry.state = "committed"
                committed.append(
                    CommittedTxn(
                        txn=txn,
                        begin_lsn=entry.begin_lsn,
                        write_lsn=entry.write_lsn,
                        commit_lsn=lsn,
                        image=entry.image,
                    )
                )
            elif rec_type == "checkpoint":
                commit_lsn = rec.get("commit_lsn")
                if isinstance(commit_lsn, bool) or not isinstance(commit_lsn, int) or commit_lsn <= 0:
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        "invalid commit_lsn in CHECKPOINT record"
                    )
                known_commit = any(c.commit_lsn == commit_lsn for c in committed)
                if not known_commit and commit_lsn > snapshot_wal_lsn:
                    raise StorageError(
                        f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                        f"CHECKPOINT references unknown or uncommitted LSN {commit_lsn}"
                    )
        except StorageError:
            raise

        prev_lsn = rec["lsn"]
        records.append(rec)
        valid_bytes_end = record_end
        offset = record_end

    discarded = sum(1 for entry in seen_txns.values() if entry.state != "committed")
    return WalAnalysis(
        records=records,
        committed=committed,
        max_lsn=prev_lsn,
        ignored_trailing=ignored_trailing,
        discarded_uncommitted=discarded,
        valid_bytes_end=valid_bytes_end,
    )


def _write_all(fd: int, data: bytes) -> int:
    """Write ``data`` completely to ``fd``, looping over short writes.

    Returns the number of bytes written. A short write is never treated as a
    complete record; the loop continues until every byte is written or the OS
    reports an error. The optional ``_AAYSQL_TEST_WAL_WRITE_LIMIT`` environment
    variable (test-only) caps each individual ``os.write`` call to exercise the
    short-write path.
    """
    limit = 0
    raw_limit = os.environ.get(_WAL_WRITE_LIMIT_ENV_VAR)
    if raw_limit:
        try:
            limit = max(1, int(raw_limit))
        except ValueError:
            limit = 0

    view = memoryview(data)
    written = 0
    while written < len(view):
        chunk = view[written : written + limit if limit else None]
        count = os.write(fd, chunk)
        if count <= 0:
            raise OSError(errno.EIO, "WAL write made no progress")
        written += count
    return written


class WalManager:
    """Manages append-only WAL writes, fsync durability, checkpoints, and truncation.

    ``state`` names the last append-machine transition (``idle``,
    ``begin_appended``, ``write_appended``, ``commit_attempted``,
    ``commit_durable``, ``checkpoint_durable``, ``damaged``, ``ambiguous``).
    ``poisoned`` becomes true whenever an append may have left torn or uncertain
    bytes behind; a poisoned manager refuses further appends until the database
    is reopened and recovery repairs the log.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.wal_path = Path(f"{self.db_path}.wal")
        self.last_lsn: int = 0
        self.poisoned: bool = False
        self.state: str = "idle"

    @property
    def requires_recovery(self) -> bool:
        return self.poisoned

    def _ensure_writable(self) -> None:
        if self.poisoned:
            raise StorageError(
                "WAL is in a damaged or uncertain state after a failed append; "
                "reopen the database to recover before writing"
            )

    def _open_append(self) -> int:
        return os.open(
            str(self.wal_path),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o644,
        )

    def append_commit_transaction(self, image_payload: dict[str, Any]) -> tuple[int, str]:
        """Write BEGIN, WRITE, COMMIT records and fsync the WAL before checkpointing.

        Returns ``(commit_lsn, txn_id)`` only after the WAL ``os.fsync()``
        succeeds. Raises ``StorageError`` when the failure happened before the
        COMMIT record could be made durable (the transaction is not committed
        and the WAL is poisoned), and ``AmbiguousCommitError`` when the COMMIT
        bytes were written but their durability is unknown.
        """
        self._ensure_writable()
        if self.wal_path.exists() and self.wal_path.is_dir():
            raise StorageError(f"WAL path '{self.wal_path}' is a directory")

        txn_id = f"txn-{uuid.uuid4().hex}"
        begin_lsn = self.last_lsn + 1
        write_lsn = self.last_lsn + 2
        commit_lsn = self.last_lsn + 3

        committed_image = dict(image_payload)
        committed_image["wal_lsn"] = commit_lsn

        try:
            begin_bytes = serialize_record(build_record(begin_lsn, txn_id, "begin"))
            write_bytes = serialize_record(
                build_record(write_lsn, txn_id, "write", image=committed_image)
            )
            commit_bytes = serialize_record(
                build_record(commit_lsn, txn_id, "commit", write_lsn=write_lsn)
            )
        except Exception as exc:
            raise StorageError(
                f"Failed to serialize WAL records for '{self.wal_path}': {exc}"
            ) from exc

        parent = self.wal_path.parent
        created_new = not self.wal_path.exists()
        try:
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Failed to create WAL directory '{parent}': {exc}") from exc

        fd: int | None = None
        try:
            fd = self._open_append()

            self.state = "begin_appended"
            _write_all(fd, begin_bytes)
            self.last_lsn = begin_lsn
            _maybe_crash("after_begin")

            self.state = "write_appended"
            _write_all(fd, write_bytes)
            self.last_lsn = write_lsn
            if os.environ.get(_CRASH_ENV_VAR) == "after_write_before_commit":
                os.fsync(fd)
                if created_new:
                    fsync_dir(parent)
                os._exit(99)

            if os.environ.get(_CRASH_ENV_VAR) == "partial_commit_record":
                partial_len = max(1, len(commit_bytes) // 2)
                partial = commit_bytes[:partial_len].rstrip(b"\n")
                if not partial:
                    partial = b"{"
                _write_all(fd, partial)
                os.fsync(fd)
                if created_new:
                    fsync_dir(parent)
                os._exit(99)

            self.state = "commit_attempted"
            _write_all(fd, commit_bytes)
            os.fsync(fd)
            self.state = "commit_durable"
            self.last_lsn = commit_lsn
        except OSError as exc:
            # Any failure after the first append may have left partial bytes on
            # disk. Never append behind them: poison the manager so only a
            # reopen + recovery can continue.
            self.poisoned = True
            if self.state == "commit_attempted":
                self.state = "ambiguous"
                raise AmbiguousCommitError(
                    f"Commit durability is ambiguous because WAL fsync failed on "
                    f"'{self.wal_path}': {exc}; reopen the database to recover"
                ) from exc
            self.state = "damaged"
            raise StorageError(
                f"Failed to append WAL records to '{self.wal_path}': {exc}; "
                "the WAL may contain an incomplete record and is blocked until recovery"
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        if created_new:
            try:
                fsync_dir(parent)
            except StorageError as exc:
                # The COMMIT bytes are fsynced, but the directory entry that
                # names the WAL may not be durable: the outcome is not
                # definitely rolled back, so report it as ambiguous.
                self.poisoned = True
                self.state = "ambiguous"
                raise AmbiguousCommitError(
                    f"Commit durability is ambiguous because the WAL directory entry "
                    f"could not be made durable for '{self.wal_path}': {exc}; "
                    "reopen the database to recover"
                ) from exc

        self.state = "idle"
        _maybe_crash("after_commit_fsync")
        return commit_lsn, txn_id

    def append_checkpoint(self, commit_lsn: int, txn_id: str | None = None) -> int:
        """Append and fsync a CHECKPOINT record referencing ``commit_lsn``."""
        self._ensure_writable()
        ckpt_lsn = max(self.last_lsn, commit_lsn) + 1
        txn = txn_id or f"ckpt-{commit_lsn}"
        rec_bytes = serialize_record(
            build_record(ckpt_lsn, txn, "checkpoint", commit_lsn=commit_lsn)
        )
        created_new = not self.wal_path.exists()
        fd: int | None = None
        try:
            fd = self._open_append()
            self.state = "checkpoint_append"
            _write_all(fd, rec_bytes)
            os.fsync(fd)
            self.last_lsn = ckpt_lsn
            self.state = "checkpoint_durable"
        except OSError as exc:
            self.poisoned = True
            self.state = "damaged"
            raise StorageError(
                f"Failed to append CHECKPOINT to '{self.wal_path}': {exc}"
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        if created_new:
            try:
                fsync_dir(self.wal_path.parent)
            except StorageError:
                self.poisoned = True
                self.state = "damaged"
                raise
        _maybe_crash("after_checkpoint_before_truncate")
        self.state = "idle"
        return ckpt_lsn

    def repair_trailing(self, valid_bytes_end: int) -> None:
        """Drop only the invalid trailing bytes, keeping every valid record.

        Used before recovery appends a CHECKPOINT so a repaired tail can never
        become mid-log corruption on a later restart. The truncation is flushed
        and fsynced before returning; on failure the WAL is poisoned.
        """
        if not self.wal_path.exists():
            return
        try:
            size = self.wal_path.stat().st_size
        except OSError as exc:
            raise StorageError(f"Failed to stat WAL '{self.wal_path}': {exc}") from exc
        if valid_bytes_end >= size:
            return
        fd: int | None = None
        try:
            fd = os.open(str(self.wal_path), os.O_WRONLY)
            os.ftruncate(fd, valid_bytes_end)
            os.fsync(fd)
        except OSError as exc:
            self.poisoned = True
            self.state = "damaged"
            raise StorageError(
                f"Failed to repair trailing WAL bytes in '{self.wal_path}': {exc}"
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            fsync_dir(self.wal_path.parent)
        except StorageError:
            self.poisoned = True
            self.state = "damaged"
            raise

    def truncate(self) -> None:
        """Truncate the WAL to 0 bytes and fsync file and directory."""
        if not self.wal_path.exists():
            return
        fd: int | None = None
        try:
            fd = os.open(str(self.wal_path), os.O_WRONLY)
            os.ftruncate(fd, 0)
            _maybe_crash("during_wal_truncation")
            os.fsync(fd)
        except OSError as exc:
            self.poisoned = True
            self.state = "damaged"
            raise StorageError(
                f"Failed to truncate WAL '{self.wal_path}': {exc}"
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        fsync_dir(self.wal_path.parent)

    def remove_if_empty(self) -> None:
        """Remove the WAL file on clean shutdown when already empty (0 bytes)."""
        if not self.wal_path.exists() or self.wal_path.is_dir():
            return
        try:
            if self.wal_path.stat().st_size == 0:
                self.wal_path.unlink()
                fsync_dir(self.wal_path.parent)
        except (OSError, StorageError):
            # Best-effort cleanup of an already-empty, fully checkpointed WAL.
            # Failure here cannot affect committed data.
            pass
