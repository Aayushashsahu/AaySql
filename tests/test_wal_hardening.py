"""v0.6.1 WAL recovery hardening: corrupt tails, directory fsync errors, partial appends.

These tests target three failure modes:

1. Recovery appending a CHECKPOINT behind an un-repaired corrupt tail, turning a
   recoverable trailing fault into mid-log corruption after a crash.
2. ``fsync_dir()`` swallowing genuine I/O errors such as ``EIO``/``ENOSPC``.
3. Buffered appends leaving partial records on disk while the engine believed the
   transaction was cleanly rolled back and kept writing behind the damage.
"""

import errno
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.errors import AmbiguousCommitError, StorageError
from aaysql.storage import JsonFileStorage
from aaysql.wal import build_record, fsync_dir, parse_wal, serialize_record

SRC_DIR = str(Path(__file__).parents[1] / "src")
CRASH_EXIT_CODE = 99

SETUP_SQL = [
    "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT NOT NULL, balance INT)",
    "AAYUSH CREATE INDEX idx_accounts_owner ON accounts (owner)",
    "AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000), (2, 'Alex', 500)",
]

BASE_ROWS = [
    {"id": 1, "owner": "Aayush", "balance": 1000},
    {"id": 2, "owner": "Alex", "balance": 500},
]


# --------------------------------------------------------------------------- helpers


def _rows(engine: Engine) -> list[dict]:
    return engine.execute(
        "AAYUSH SELECT id, owner, balance FROM accounts ORDER BY id ASC"
    )["rows"]


def _make_database(db_file: Path) -> None:
    with Engine(db_file) as engine:
        for stmt in SETUP_SQL:
            engine.execute(stmt)


def _append_committed_wal_txn(db_file: Path, mutate) -> int:
    """Commit ``mutate(db)`` straight into the WAL without checkpointing it."""
    storage = JsonFileStorage(db_file)
    db = storage.load()
    mutate(db)
    commit_lsn, _ = storage.wal.append_commit_transaction(db.to_dict())
    return commit_lsn


def _append_raw(wal_path: Path, payload: bytes) -> None:
    with open(wal_path, "ab") as handle:
        handle.write(payload)


def _run_child(
    db_file: Path,
    *,
    crash_point: str | None = None,
    env: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess:
    """Run a child process that opens ``db_file`` under injected conditions."""
    if body is None:
        body = """
import os, sys
db_path = sys.argv[1]
if sys.argv[2] != "-":
    os.environ["_AAYSQL_TEST_CRASH_POINT"] = sys.argv[2]
from aaysql import Engine
engine = Engine(db_path)
engine.close()
raise AssertionError("expected the child to terminate at the crash point")
"""
    env_vars = os.environ.copy()
    env_vars["PYTHONPATH"] = SRC_DIR + (
        os.pathsep + env_vars["PYTHONPATH"] if env_vars.get("PYTHONPATH") else ""
    )
    env_vars.pop("_AAYSQL_TEST_CRASH_POINT", None)
    env_vars.pop("_AAYSQL_TEST_DIR_FSYNC_ERROR", None)
    env_vars.pop("_AAYSQL_TEST_WAL_WRITE_LIMIT", None)
    if env:
        env_vars.update(env)
    return subprocess.run(
        [sys.executable, "-c", body, str(db_file), crash_point or "-"],
        cwd=db_file.parent,
        env=env_vars,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _assert_clean_database(db_file: Path, expected_rows: list[dict]) -> None:
    with Engine(db_file) as engine:
        assert _rows(engine) == expected_rows
        assert "idx_accounts_owner" in engine.db.indexes
        engine.db.indexes["idx_accounts_owner"].validate()
    # A second recovery pass must be a no-op.
    with Engine(db_file) as engine:
        assert _rows(engine) == expected_rows
    assert not Path(f"{db_file}.wal").exists() or Path(f"{db_file}.wal").stat().st_size == 0


# --------------------------------------------------- Phase 1: trailing-tail repair


def test_parse_wal_reports_byte_boundary_with_multibyte_utf8_content(tmp_path: Path):
    wal_path = tmp_path / "utf8.wal"
    txn = "txn-utf8"
    image = {"owner": "Aayush ✓", "rows": [{"note": "héllo"}]}
    valid = (
        serialize_record(build_record(1, txn, "begin"))
        + serialize_record(build_record(2, txn, "write", image=image))
        + serialize_record(build_record(3, txn, "commit", write_lsn=2))
    )
    torn = b'{"lsn":4,"txn":"txn-torn","type":"beg'
    wal_path.write_bytes(valid + torn)

    analysis = parse_wal(wal_path)
    assert analysis.ignored_trailing is True
    assert analysis.valid_bytes_end == len(valid), "offset must be in bytes, not characters"
    assert analysis.max_lsn == 3
    assert [c.commit_lsn for c in analysis.committed] == [3]
    # The reported boundary is exactly where the torn tail starts.
    assert wal_path.read_bytes()[analysis.valid_bytes_end :] == torn


def test_parse_wal_clean_file_boundary_covers_entire_log(tmp_path: Path):
    wal_path = tmp_path / "clean.wal"
    payload = serialize_record(build_record(1, "txn-a", "begin"))
    wal_path.write_bytes(payload)
    analysis = parse_wal(wal_path)
    assert analysis.ignored_trailing is False
    assert analysis.valid_bytes_end == len(payload)


def test_recovery_repairs_tail_before_checkpoint_and_replays_once(tmp_path: Path):
    db_file = tmp_path / "repair.aaydb"
    _make_database(db_file)
    _append_committed_wal_txn(
        db_file,
        lambda db: db.tables["accounts"].rows.append(
            {"id": 3, "owner": "Mika", "balance": 250}
        ),
    )
    wal_path = Path(f"{db_file}.wal")
    corrupt_tail = b'{"lsn":999,"txn":"txn-corrupt","type":"begin"'
    _append_raw(wal_path, corrupt_tail)
    assert wal_path.read_bytes().endswith(corrupt_tail)

    with Engine(db_file) as engine:
        assert _rows(engine) == BASE_ROWS + [{"id": 3, "owner": "Mika", "balance": 250}]
    # The whole WAL is consumed by the checkpoint after repair.
    assert not wal_path.exists() or wal_path.stat().st_size == 0
    _assert_clean_database(
        db_file, BASE_ROWS + [{"id": 3, "owner": "Mika", "balance": 250}]
    )


def test_corrupt_tail_before_checkpoint_crash_never_becomes_mid_log_corruption(
    tmp_path: Path,
):
    """Regression: the exact seven-step failure sequence from the v0.6.0 audit.

    1-2. A valid committed transaction sits in the WAL with a corrupt tail.
    3-4. Recovery ignores the tail and replays the transaction.
    5.   Recovery appends CHECKPOINT.
    6.   The process crashes before truncating the WAL.
    7.   Reopening must not see a corrupt record in the middle of the log.
    Before v0.6.1 step 5 appended behind the corrupt bytes, so step 7 raised
    StorageError. Recovery now repairs the tail first.
    """
    db_file = tmp_path / "seven_steps.aaydb"
    _make_database(db_file)
    _append_committed_wal_txn(
        db_file,
        lambda db: db.tables["accounts"].rows.append(
            {"id": 3, "owner": "Mika", "balance": 250}
        ),
    )
    # A newline-terminated but checksum-invalid record is the dangerous variant:
    # appending CHECKPOINT after it would place a corrupt record in the middle of
    # the log, because the tail is a complete line rather than an unterminated one.
    _append_raw(Path(f"{db_file}.wal"), b'{"corrupt":true}\n')

    proc = _run_child(db_file, crash_point="after_checkpoint_before_truncate")
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr
    assert Path(f"{db_file}.wal").stat().st_size > 0

    expected = BASE_ROWS + [{"id": 3, "owner": "Mika", "balance": 250}]
    _assert_clean_database(db_file, expected)
    _assert_clean_database(db_file, expected)


def test_subprocess_crash_after_tail_repair_replays_exactly_once(tmp_path: Path):
    db_file = tmp_path / "tail_repair_crash.aaydb"
    _make_database(db_file)
    _append_committed_wal_txn(
        db_file,
        lambda db: db.tables["accounts"].rows.append(
            {"id": 7, "owner": "Repaired", "balance": 70}
        ),
    )
    _append_raw(Path(f"{db_file}.wal"), b'{"lsn":50,"txn":"torn"')

    proc = _run_child(db_file, crash_point="after_tail_repair")
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr

    expected = BASE_ROWS + [{"id": 7, "owner": "Repaired", "balance": 70}]
    _assert_clean_database(db_file, expected)
    _assert_clean_database(db_file, expected)


def test_tail_repair_preserves_multiple_valid_committed_records(tmp_path: Path):
    db_file = tmp_path / "multi_repair.aaydb"
    _make_database(db_file)

    def first(db):
        db.tables["accounts"].rows.append({"id": 3, "owner": "One", "balance": 1})

    def second(db):
        db.tables["accounts"].rows[0]["balance"] = 1111
        db.tables["accounts"].rows.append({"id": 4, "owner": "Two", "balance": 2})

    lsn1 = _append_committed_wal_txn(db_file, first)
    lsn2 = _append_committed_wal_txn(db_file, second)
    assert lsn2 > lsn1
    wal_path = Path(f"{db_file}.wal")
    valid_bytes = wal_path.read_bytes()
    _append_raw(wal_path, b"\x00\x01 not json\n")

    with Engine(db_file) as engine:
        assert _rows(engine) == [
            {"id": 1, "owner": "Aayush", "balance": 1111},
            {"id": 2, "owner": "Alex", "balance": 500},
            {"id": 3, "owner": "One", "balance": 1},
            {"id": 4, "owner": "Two", "balance": 2},
        ]
    # Repair kept every byte of the valid prefix and consumed the rest.
    assert len(valid_bytes) > 0
    _assert_clean_database(
        db_file,
        [
            {"id": 1, "owner": "Aayush", "balance": 1111},
            {"id": 2, "owner": "Alex", "balance": 500},
            {"id": 3, "owner": "One", "balance": 1},
            {"id": 4, "owner": "Two", "balance": 2},
        ],
    )


def test_recovery_discards_uncommitted_transaction_with_corrupt_tail(tmp_path: Path):
    db_file = tmp_path / "uncommitted_tail.aaydb"
    _make_database(db_file)
    storage = JsonFileStorage(db_file)
    db = storage.load()
    wal_path = storage.wal.wal_path
    _append_raw(
        wal_path,
        serialize_record(build_record(storage.wal.last_lsn + 1, "txn-ghost", "begin"))
        + serialize_record(
            build_record(
                storage.wal.last_lsn + 2,
                "txn-ghost",
                "write",
                image={**db.to_dict(), "wal_lsn": storage.wal.last_lsn + 3},
            )
        )
        + b'{"torn":',
    )

    with Engine(db_file) as engine:
        assert _rows(engine) == BASE_ROWS
    assert not wal_path.exists() or wal_path.stat().st_size == 0


def test_mid_log_corruption_after_valid_records_still_refuses_open(tmp_path: Path):
    db_file = tmp_path / "mid_log.aaydb"
    _make_database(db_file)
    storage = JsonFileStorage(db_file)
    db = storage.load()
    wal_path = storage.wal.wal_path
    commit_lsn, _ = storage.wal.append_commit_transaction(db.to_dict())

    snapshot_before = db_file.read_bytes()
    corrupt = build_record(commit_lsn + 1, "txn-bad", "begin")
    corrupt["checksum"] = "a" * 64
    later = build_record(commit_lsn + 2, "txn-later", "begin")
    _append_raw(wal_path, serialize_record(corrupt) + serialize_record(later))

    with pytest.raises(StorageError, match="Corrupted WAL record"):
        Engine(db_file)
    # The snapshot is untouched, the later record was not applied, and the
    # corruption keeps refusing to open instead of being skipped.
    assert db_file.read_bytes() == snapshot_before
    with pytest.raises(StorageError, match="Corrupted WAL record"):
        Engine(db_file)


# ------------------------------------------- Phase 2: directory fsync error handling


def test_fsync_dir_calls_os_fsync_when_supported(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    class FakeFD:
        pass

    monkeypatch.setattr("aaysql.wal._directory_fsync_supported", lambda: True)
    monkeypatch.setattr("aaysql.wal.os.open", lambda path, flags: FakeFD())
    monkeypatch.setattr("aaysql.wal.os.fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr("aaysql.wal.os.close", lambda fd: calls.append("close"))

    fsync_dir(tmp_path)
    assert calls == ["fsync", "close"]


def test_fsync_dir_skips_when_platform_unsupported(tmp_path: Path, monkeypatch):
    opened = []
    monkeypatch.setattr("aaysql.wal._directory_fsync_supported", lambda: False)
    monkeypatch.setattr(
        "aaysql.wal.os.open", lambda path, flags: opened.append(path) or 1
    )
    fsync_dir(tmp_path)
    assert opened == []


def test_fsync_dir_unsupported_errno_falls_back(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("aaysql.wal._directory_fsync_supported", lambda: True)

    def unsupported_open(path, flags):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr("aaysql.wal.os.open", unsupported_open)
    fsync_dir(tmp_path)  # must not raise


def test_fsync_dir_permission_error_raises_storage_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("aaysql.wal._directory_fsync_supported", lambda: True)

    def denied(path, flags):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("aaysql.wal.os.open", denied)
    with pytest.raises(StorageError, match="Failed to open directory"):
        fsync_dir(tmp_path)


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC])
def test_fsync_dir_genuine_failure_raises_storage_error(
    tmp_path: Path, monkeypatch, code: int
):
    closed: list[bool] = []
    monkeypatch.setattr("aaysql.wal._directory_fsync_supported", lambda: True)
    monkeypatch.setattr("aaysql.wal.os.open", lambda path, flags: 123)

    def failing_fsync(fd):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr("aaysql.wal.os.fsync", failing_fsync)
    monkeypatch.setattr("aaysql.wal.os.close", lambda fd: closed.append(True))

    with pytest.raises(StorageError, match="Failed to fsync directory"):
        fsync_dir(tmp_path)
    assert closed == [True], "descriptor must be closed even when fsync fails"


def test_checkpoint_dir_fsync_failure_keeps_commit_and_wal(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "dir_fsync_commit.aaydb"
    _make_database(db_file)
    engine = Engine(db_file)

    def failing_dir_fsync(path):
        raise StorageError("Failed to fsync directory: simulated EIO")

    monkeypatch.setattr("aaysql.storage.fsync_dir", failing_dir_fsync)
    result = engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Durable', 300)")
    assert result["status"] == "ok"
    assert engine.storage.pending_checkpoint is True
    wal_path = Path(f"{db_file}.wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0
    engine.close()
    assert wal_path.exists() and wal_path.stat().st_size > 0

    monkeypatch.undo()
    _assert_clean_database(
        db_file, BASE_ROWS + [{"id": 3, "owner": "Durable", "balance": 300}]
    )


def test_subprocess_directory_fsync_error_preserves_wal_and_recovers(tmp_path: Path):
    """Scenario 2: a real EIO during checkpoint directory fsync, then a crash."""
    db_file = tmp_path / "dir_fsync_crash.aaydb"
    _make_database(db_file)
    _append_committed_wal_txn(
        db_file,
        lambda db: db.tables["accounts"].rows.append(
            {"id": 3, "owner": "Durable", "balance": 300}
        ),
    )
    body = """
import os, sys
db_path = sys.argv[1]
os.environ["_AAYSQL_TEST_DIR_FSYNC_ERROR"] = "EIO"
from aaysql import Engine
from aaysql.errors import StorageError
try:
    Engine(db_path)
except StorageError as exc:
    wal = db_path + ".wal"
    assert os.path.exists(wal), "WAL removed after durability failure"
    assert os.path.getsize(wal) > 0, "WAL truncated after durability failure"
    assert "fsync" in str(exc)
    os._exit(99)
os._exit(98)
"""
    proc = _run_child(db_file, body=body)
    assert proc.returncode == CRASH_EXIT_CODE, (proc.returncode, proc.stderr)

    expected = BASE_ROWS + [{"id": 3, "owner": "Durable", "balance": 300}]
    _assert_clean_database(db_file, expected)
    _assert_clean_database(db_file, expected)


# ------------------------------------------------- Phase 3: partial append handling


def test_short_writes_are_completed_by_write_all(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "short_write.aaydb"
    _make_database(db_file)
    storage = JsonFileStorage(db_file)
    db = storage.load()
    monkeypatch.setenv("_AAYSQL_TEST_WAL_WRITE_LIMIT", "3")

    commit_lsn, txn = storage.wal.append_commit_transaction(db.to_dict())
    analysis = parse_wal(storage.wal.wal_path)
    raw = storage.wal.wal_path.read_bytes()
    assert analysis.valid_bytes_end == len(raw)
    assert analysis.ignored_trailing is False
    assert [c.commit_lsn for c in analysis.committed] == [commit_lsn]
    assert analysis.max_lsn == commit_lsn


def test_short_write_then_error_poisons_wal_and_restores_state(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "partial_fail.aaydb"
    _make_database(db_file)
    engine = Engine(db_file)
    real_write = os.write
    budget = {"left": 20}

    def failing_write(fd, data):
        if budget["left"] <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        chunk = bytes(data)[: budget["left"]]
        written = real_write(fd, chunk)
        budget["left"] -= written
        return written

    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Halved', 1)")
    monkeypatch.setattr("aaysql.wal.os.write", failing_write)
    with pytest.raises(StorageError, match="Failed to append WAL records"):
        engine.execute("AAYUSH COMMIT")
    monkeypatch.undo()

    assert engine.needs_recovery is True
    assert engine.storage.wal.poisoned is True
    assert engine.storage.wal.state == "damaged"
    # In-memory state matches the pre-transaction snapshot (engine reads are
    # blocked, so inspect the installed database directly).
    assert engine.db.tables["accounts"].rows == BASE_ROWS
    # The engine refuses to append behind the damaged tail.
    with pytest.raises(StorageError, match="reopen the database"):
        engine.execute("AAYUSH INSERT INTO accounts VALUES (9, 'Unsafe', 1)")
    engine.close()

    # Reopening discards the torn transaction and allows new work.
    with Engine(db_file) as reopened:
        assert _rows(reopened) == BASE_ROWS
        reopened.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Fresh', 4)")
    _assert_clean_database(db_file, BASE_ROWS + [{"id": 4, "owner": "Fresh", "balance": 4}])


def test_failure_during_begin_append_restores_state_and_poisons(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "begin_fail.aaydb"
    _make_database(db_file)
    engine = Engine(db_file)

    def immediate_failure(fd, data):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr("aaysql.wal.os.write", immediate_failure)
    with pytest.raises(StorageError):
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Nope', 1)")
    monkeypatch.undo()

    assert engine.needs_recovery is True
    assert engine.db.tables["accounts"].rows == BASE_ROWS
    engine.close()
    with Engine(db_file) as reopened:
        assert _rows(reopened) == BASE_ROWS


def test_commit_fsync_failure_is_ambiguous_and_keeps_in_memory_state(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "ambiguous_new.aaydb"
    _make_database(db_file)
    engine = Engine(db_file)

    def failing_fsync(fd):
        raise OSError(errno.EIO, "I/O error during fsync")

    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Maybe', 600)")
    monkeypatch.setattr("aaysql.wal.os.fsync", failing_fsync)
    with pytest.raises(AmbiguousCommitError):
        engine.execute("AAYUSH COMMIT")
    monkeypatch.undo()

    assert engine.needs_recovery is True
    assert engine.storage.wal.state == "ambiguous"
    # Not rolled back: the commit outcome is unknown, not definitively failed.
    assert len(engine.db.tables["accounts"].rows) == 3
    engine.close()

    # The fully written COMMIT record makes the transaction recoverable.
    with Engine(db_file) as reopened:
        assert _rows(reopened) == BASE_ROWS + [{"id": 3, "owner": "Maybe", "balance": 600}]


def test_poisoned_manager_refuses_further_appends(tmp_path: Path):
    db_file = tmp_path / "poisoned.aaydb"
    _make_database(db_file)
    storage = JsonFileStorage(db_file)
    db = storage.load()
    storage.wal.poisoned = True
    with pytest.raises(StorageError, match="damaged or uncertain"):
        storage.wal.append_commit_transaction(db.to_dict())
    with pytest.raises(StorageError, match="damaged or uncertain"):
        storage.wal.append_checkpoint(1)


def test_checkpoint_append_failure_blocks_writes_until_reopen(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "ckpt_append_fail.aaydb"
    _make_database(db_file)
    engine = Engine(db_file)

    def poisoned_checkpoint(*args, **kwargs):
        engine.storage.wal.poisoned = True
        engine.storage.wal.state = "damaged"
        raise StorageError("Failed to append CHECKPOINT")

    monkeypatch.setattr(engine.storage.wal, "append_checkpoint", poisoned_checkpoint)
    result = engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Committed', 3)")
    assert result["status"] == "ok"  # WAL commit was already durable
    assert engine.storage.pending_checkpoint is True

    with pytest.raises(StorageError, match="reopen the database"):
        engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Blocked', 4)")
    engine.close()

    monkeypatch.undo()
    _assert_clean_database(
        db_file, BASE_ROWS + [{"id": 3, "owner": "Committed", "balance": 3}]
    )


def test_partial_commit_record_never_becomes_visible_and_log_stays_appendable(
    tmp_path: Path,
):
    """Scenario 3: a torn COMMIT is discarded, and recovery leaves a clean log."""
    db_file = tmp_path / "partial_commit.aaydb"
    _make_database(db_file)
    body = """
import json, os, sys
db_path = sys.argv[1]
from aaysql import Engine
engine = Engine(db_path)
os.environ["_AAYSQL_TEST_CRASH_POINT"] = "partial_commit_record"
engine.execute("AAYUSH BEGIN")
engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Torn', 1)")
engine.execute("AAYUSH COMMIT")
raise AssertionError("expected crash")
"""
    proc = _run_child(db_file, body=body)
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr
    wal_path = Path(f"{db_file}.wal")
    assert wal_path.stat().st_size > 0

    with Engine(db_file) as engine:
        assert _rows(engine) == BASE_ROWS
        # No valid COMMIT exists, so nothing was applied. The log was repaired,
        # so a new transaction may append safely.
        engine.execute("AAYUSH INSERT INTO accounts VALUES (5, 'After', 5)")

    _assert_clean_database(db_file, BASE_ROWS + [{"id": 5, "owner": "After", "balance": 5}])
    assert parse_wal(wal_path).committed == [] if wal_path.exists() else True


# ------------------------------------- Phase 5: combined adversarial crash scenarios


def test_subprocess_multiple_commits_with_corrupt_tail_and_checkpoint_crash(
    tmp_path: Path,
):
    """Scenario 4: several committed transactions, corrupt tail, crash at checkpoint."""
    db_file = tmp_path / "multi_crash.aaydb"
    _make_database(db_file)
    _append_committed_wal_txn(
        db_file,
        lambda db: db.tables["accounts"].rows.append(
            {"id": 3, "owner": "First", "balance": 10}
        ),
    )
    _append_committed_wal_txn(
        db_file,
        lambda db: db.tables["accounts"].rows.append(
            {"id": 4, "owner": "Second", "balance": 20}
        ),
    )
    _append_raw(Path(f"{db_file}.wal"), b'{"garbage":"tail"}\n')

    proc = _run_child(db_file, crash_point="after_checkpoint_before_truncate")
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr

    expected = BASE_ROWS + [
        {"id": 3, "owner": "First", "balance": 10},
        {"id": 4, "owner": "Second", "balance": 20},
    ]
    _assert_clean_database(db_file, expected)
    _assert_clean_database(db_file, expected)
    _assert_clean_database(db_file, expected)


def test_subprocess_mid_log_corruption_refuses_open_and_preserves_snapshot(tmp_path: Path):
    """Scenario 5: corruption before later records must abort, not skip forward."""
    db_file = tmp_path / "mid_log_subprocess.aaydb"
    _make_database(db_file)
    snapshot_before = db_file.read_bytes()
    storage = JsonFileStorage(db_file)
    db = storage.load()
    wal_path = storage.wal.wal_path
    commit_lsn, _ = storage.wal.append_commit_transaction(db.to_dict())

    corrupt = build_record(commit_lsn + 1, "txn-bad", "begin")
    corrupt["checksum"] = "0" * 64
    later_image = {**db.to_dict(), "wal_lsn": commit_lsn + 4}
    later_image["tables"]["accounts"]["rows"].append(
        {"id": 9, "owner": "Later", "balance": 9}
    )
    later = (
        serialize_record(build_record(commit_lsn + 2, "txn-later", "begin"))
        + serialize_record(build_record(commit_lsn + 3, "txn-later", "write", image=later_image))
        + serialize_record(build_record(commit_lsn + 4, "txn-later", "commit", write_lsn=commit_lsn + 3))
    )
    _append_raw(wal_path, serialize_record(corrupt) + later)

    body = """
import os, sys
db_path = sys.argv[1]
from aaysql import Engine
from aaysql.errors import StorageError
try:
    Engine(db_path)
except StorageError as exc:
    if "Corrupted WAL record" not in str(exc):
        raise
    os._exit(99)
os._exit(98)
"""
    proc = _run_child(db_file, body=body)
    assert proc.returncode == CRASH_EXIT_CODE, (proc.returncode, proc.stderr)
    assert db_file.read_bytes() == snapshot_before
    with pytest.raises(StorageError, match="Corrupted WAL record"):
        Engine(db_file)
