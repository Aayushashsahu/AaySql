"""Write-Ahead Log (WAL) protocol, integrity verification, and durability helpers."""

from __future__ import annotations

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


def _maybe_crash(point: str) -> None:
    """Internal crash-testing hook. Terminates immediately via os._exit without cleanup."""
    if os.environ.get(_CRASH_ENV_VAR) == point:
        os._exit(99)


def _reject_non_finite(token: str) -> Any:
    raise ValueError(f"Non-finite JSON constant '{token}' is not allowed in WAL records")


def fsync_dir(dir_path: Path) -> None:
    """Fsync a directory entry where supported by the operating system.

    POSIX filesystems (Linux, macOS) support opening a directory with O_RDONLY and
    calling os.fsync() to durably persist newly created or renamed directory entries.
    Windows does not support os.open() on directories for fsync, so OSError is
    handled gracefully there.
    """
    target = dir_path if str(dir_path) else Path(".")
    flags = os.O_RDONLY
    try:
        fd = os.open(str(target), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
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


def parse_wal(wal_path: Path, snapshot_wal_lsn: int = 0) -> WalAnalysis:
    """Read, validate, and analyze WAL records from disk.

    Stops at the first invalid or incomplete record. Trailing incomplete or
    corrupt records (at the very end of the WAL) are safely ignored as
    crash-torn writes. Any invalid/corrupt record *before* subsequent data raises
    StorageError so corruption is never silently skipped.
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
        )

    lines = raw_bytes.splitlines(keepends=True)
    prev_lsn = 0
    seen_txns: dict[str, _TxnEntry] = {}
    records: list[dict[str, Any]] = []
    committed: list[CommittedTxn] = []
    ignored_trailing = False

    for idx, raw_line in enumerate(lines):
        is_last = idx == len(lines) - 1

        def handle_invalid(reason: str, *, _is_last: bool = is_last, _idx: int = idx) -> None:
            nonlocal ignored_trailing
            if _is_last:
                ignored_trailing = True
                return
            raise StorageError(
                f"Corrupted WAL record at line {_idx + 1} in '{wal_path}': {reason}"
            )

        if not raw_line.endswith(b"\n"):
            handle_invalid("incomplete record missing trailing newline")
            break

        try:
            text_line = raw_line[:-1].decode("utf-8", errors="strict")
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
                f"Corrupted WAL record at line {idx + 1} in '{wal_path}': unknown record type {rec_type!r}"
            )

        if set(rec.keys()) != _ALLOWED_KEYS[rec_type]:
            raise StorageError(
                f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                f"unexpected or missing fields for '{rec_type}' record"
            )

        lsn = rec.get("lsn")
        if isinstance(lsn, bool) or not isinstance(lsn, int) or lsn <= 0:
            raise StorageError(f"Corrupted WAL record at line {idx + 1} in '{wal_path}': invalid LSN")
        if lsn <= prev_lsn:
            raise StorageError(
                f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                f"non-monotonic LSN {lsn} after {prev_lsn}"
            )

        txn = rec.get("txn")
        if not isinstance(txn, str) or not txn:
            raise StorageError(
                f"Corrupted WAL record at line {idx + 1} in '{wal_path}': invalid transaction identifier"
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
            if entry is None or entry.state != "written" or entry.write_lsn is None or entry.image is None:
                raise StorageError(
                    f"Corrupted WAL record at line {idx + 1} in '{wal_path}': "
                    f"COMMIT without matching WRITE for transaction '{txn}'"
                )
            write_lsn = rec.get("write_lsn")
            if isinstance(write_lsn, bool) or not isinstance(write_lsn, int) or write_lsn != entry.write_lsn:
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

        prev_lsn = lsn
        records.append(rec)

    discarded = sum(1 for entry in seen_txns.values() if entry.state != "committed")
    return WalAnalysis(
        records=records,
        committed=committed,
        max_lsn=prev_lsn,
        ignored_trailing=ignored_trailing,
        discarded_uncommitted=discarded,
    )


class WalManager:
    """Manages append-only WAL writes, fsync durability, checkpoints, and truncation."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.wal_path = Path(f"{self.db_path}.wal")
        self.last_lsn: int = 0

    def append_commit_transaction(self, image_payload: dict[str, Any]) -> tuple[int, str]:
        """Write BEGIN, WRITE, COMMIT records and fsync the WAL before checkpointing.

        Returns ``(commit_lsn, txn_id)`` only after WAL ``os.fsync()`` succeeds.
        Raises ``StorageError`` on failures before the COMMIT fsync, and
        ``AmbiguousCommitError`` if the COMMIT fsync itself fails.
        """
        if self.wal_path.exists() and self.wal_path.is_dir():
            raise StorageError(f"WAL path '{self.wal_path}' is a directory")

        txn_id = f"txn-{uuid.uuid4().hex}"
        begin_lsn = self.last_lsn + 1
        write_lsn = self.last_lsn + 2
        commit_lsn = self.last_lsn + 3

        committed_image = dict(image_payload)
        committed_image["wal_lsn"] = commit_lsn

        try:
            begin_rec = build_record(begin_lsn, txn_id, "begin")
            write_rec = build_record(write_lsn, txn_id, "write", image=committed_image)
            commit_rec = build_record(commit_lsn, txn_id, "commit", write_lsn=write_lsn)
            begin_bytes = serialize_record(begin_rec)
            write_bytes = serialize_record(write_rec)
            commit_bytes = serialize_record(commit_rec)
        except Exception as exc:
            raise StorageError(f"Failed to serialize WAL records for '{self.wal_path}': {exc}") from exc

        parent = self.wal_path.parent
        created_new = not self.wal_path.exists()
        try:
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Failed to create WAL directory '{parent}': {exc}") from exc

        wrote_commit = False
        try:
            with open(self.wal_path, "ab") as wal_file:
                wal_file.write(begin_bytes)
                wal_file.flush()
                self.last_lsn = begin_lsn
                _maybe_crash("after_begin")

                wal_file.write(write_bytes)
                wal_file.flush()
                self.last_lsn = write_lsn
                if os.environ.get(_CRASH_ENV_VAR) == "after_write_before_commit":
                    os.fsync(wal_file.fileno())
                    if created_new:
                        fsync_dir(parent)
                    os._exit(99)

                if os.environ.get(_CRASH_ENV_VAR) == "partial_commit_record":
                    partial_len = max(1, len(commit_bytes) // 2)
                    partial = commit_bytes[:partial_len].rstrip(b"\n")
                    if not partial:
                        partial = b"{"
                    wal_file.write(partial)
                    wal_file.flush()
                    os.fsync(wal_file.fileno())
                    if created_new:
                        fsync_dir(parent)
                    os._exit(99)

                wal_file.write(commit_bytes)
                wal_file.flush()
                wrote_commit = True
                self.last_lsn = commit_lsn

                os.fsync(wal_file.fileno())
                if created_new:
                    fsync_dir(parent)
        except OSError as exc:
            if wrote_commit:
                raise AmbiguousCommitError(
                    f"Commit durability is ambiguous because WAL fsync failed on '{self.wal_path}': {exc}; "
                    "reopen the database to recover"
                ) from exc
            raise StorageError(f"Failed to write WAL file '{self.wal_path}': {exc}") from exc

        _maybe_crash("after_commit_fsync")
        return commit_lsn, txn_id

    def append_checkpoint(self, commit_lsn: int, txn_id: str | None = None) -> int:
        """Append and fsync a CHECKPOINT record referencing ``commit_lsn``."""
        ckpt_lsn = max(self.last_lsn, commit_lsn) + 1
        txn = txn_id or f"ckpt-{commit_lsn}"
        rec = build_record(ckpt_lsn, txn, "checkpoint", commit_lsn=commit_lsn)
        rec_bytes = serialize_record(rec)
        created_new = not self.wal_path.exists()
        with open(self.wal_path, "ab") as wal_file:
            wal_file.write(rec_bytes)
            wal_file.flush()
            self.last_lsn = ckpt_lsn
            os.fsync(wal_file.fileno())
            if created_new:
                fsync_dir(self.wal_path.parent)
        _maybe_crash("after_checkpoint_before_truncate")
        return ckpt_lsn

    def truncate(self) -> None:
        """Truncate the WAL to 0 bytes and fsync file and directory."""
        if not self.wal_path.exists():
            return
        with open(self.wal_path, "r+b") as wal_file:
            wal_file.truncate(0)
            wal_file.flush()
            _maybe_crash("during_wal_truncation")
            os.fsync(wal_file.fileno())
        fsync_dir(self.wal_path.parent)

    def remove_if_empty(self) -> None:
        """Remove the WAL file on clean shutdown when already empty (0 bytes)."""
        if not self.wal_path.exists() or self.wal_path.is_dir():
            return
        try:
            if self.wal_path.stat().st_size == 0:
                self.wal_path.unlink()
                fsync_dir(self.wal_path.parent)
        except OSError:
            pass
