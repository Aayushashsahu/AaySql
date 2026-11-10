import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine, __version__
from aaysql.errors import AmbiguousCommitError, ConstraintError, StorageError
from aaysql.storage import ENGINE_VERSION, FORMAT_VERSION, Database, JsonFileStorage
from aaysql.wal import build_record, parse_wal, serialize_record

SRC_DIR = str(Path(__file__).parents[1] / "src")
CRASH_EXIT_CODE = 99


def _run_crash_subprocess(
    db_file: Path,
    crash_point: str,
    setup_sql: list[str],
    target_sql: list[str],
    *,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess:
    """Run a child Python process that sets up committed state, arms a crash hook, and executes target_sql."""
    script = """
import json
import os
import sys

db_path = sys.argv[1]
crash_point = sys.argv[2]
setup_sql = json.loads(sys.argv[3])
target_sql = json.loads(sys.argv[4])

from aaysql import Engine

engine = Engine(db_path)
for stmt in setup_sql:
    engine.execute(stmt)

os.environ["_AAYSQL_TEST_CRASH_POINT"] = crash_point
for stmt in target_sql:
    engine.execute(stmt)

raise AssertionError(f"Expected process to terminate at crash point {crash_point!r}")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("_AAYSQL_TEST_CRASH_POINT", None)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(db_file),
            crash_point,
            json.dumps(setup_sql),
            json.dumps(target_sql),
        ],
        cwd=db_file.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _baseline_setup() -> list[str]:
    return [
        "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT NOT NULL, balance INT)",
        "AAYUSH CREATE INDEX idx_accounts_owner ON accounts (owner)",
        "AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000), (2, 'Alex', 500)",
    ]


def _rows(engine: Engine) -> list[dict]:
    return engine.execute("AAYUSH SELECT id, owner, balance FROM accounts ORDER BY id ASC")["rows"]


def _verify_reopen_idempotency(db_file: Path, expected_rows: list[dict]) -> None:
    """Open, verify state, close, reopen, and verify identical state again."""
    with Engine(db_file) as e1:
        assert _rows(e1) == expected_rows
        assert "idx_accounts_owner" in e1.db.indexes
        e1.db.indexes["idx_accounts_owner"].validate()

    # Second reopen after recovery must produce identical state and clean directory.
    with Engine(db_file) as e2:
        assert _rows(e2) == expected_rows
        assert "idx_accounts_owner" in e2.db.indexes
        e2.db.indexes["idx_accounts_owner"].validate()
        plan = e2.execute("AAYUSH EXPLAIN SELECT id FROM accounts WHERE owner = 'Aayush'")["message"]
        assert "INDEX SCAN: idx_accounts_owner" in plan


# --- PHASE 6: Real subprocess os._exit() crash scenarios ---


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_begin",
        "after_write_before_commit",
        "partial_commit_record",
    ],
)
def test_subprocess_crash_before_commit_discards_transaction(tmp_path: Path, crash_point: str):
    db_file = tmp_path / f"{crash_point}.aaydb"
    proc = _run_crash_subprocess(
        db_file,
        crash_point,
        _baseline_setup(),
        [
            "AAYUSH BEGIN",
            "AAYUSH INSERT INTO accounts VALUES (3, 'Ghost', 999)",
            "AAYUSH UPDATE accounts SET balance = 0 WHERE id = 1",
            "AAYUSH COMMIT",
        ],
    )
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr
    assert Path(f"{db_file}.wal").exists()

    expected = [
        {"id": 1, "owner": "Aayush", "balance": 1000},
        {"id": 2, "owner": "Alex", "balance": 500},
    ]
    _verify_reopen_idempotency(db_file, expected)


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_commit_fsync",
        "during_snapshot_write",
        "after_snapshot_fsync_before_replace",
        "after_snapshot_replace",
        "after_checkpoint_before_truncate",
        "during_wal_truncation",
    ],
)
def test_subprocess_crash_after_commit_recovers_transaction(tmp_path: Path, crash_point: str):
    db_file = tmp_path / f"{crash_point}.aaydb"
    proc = _run_crash_subprocess(
        db_file,
        crash_point,
        _baseline_setup(),
        [
            "AAYUSH BEGIN",
            "AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)",
            "AAYUSH UPDATE accounts SET balance = 900 WHERE id = 1",
            "AAYUSH COMMIT",
        ],
    )
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr

    expected = [
        {"id": 1, "owner": "Aayush", "balance": 900},
        {"id": 2, "owner": "Alex", "balance": 500},
        {"id": 3, "owner": "Mika", "balance": 250},
    ]
    _verify_reopen_idempotency(db_file, expected)
    # Stale temp files and non-empty WAL must be cleaned up after recovery.
    assert [p.name for p in tmp_path.iterdir()] == [db_file.name]


def test_subprocess_crash_on_brand_new_database_before_first_snapshot(tmp_path: Path):
    db_file = tmp_path / "brand_new.aaydb"
    proc = _run_crash_subprocess(
        db_file,
        "after_commit_fsync",
        [],
        ["AAYUSH CREATE TABLE items (id INT PRIMARY KEY, label TEXT NOT NULL)"],
    )
    assert proc.returncode == CRASH_EXIT_CODE, proc.stderr
    assert not db_file.exists()
    assert Path(f"{db_file}.wal").exists()

    with Engine(db_file) as reopened:
        assert "items" in reopened.db.tables
        reopened.execute("AAYUSH INSERT INTO items VALUES (1, 'Pen')")
    with Engine(db_file) as reopened_again:
        assert reopened_again.execute("AAYUSH SELECT * FROM items")["rows"] == [
            {"id": 1, "label": "Pen"}
        ]


def test_subprocess_crash_autocommit_and_ddl_inside_transaction(tmp_path: Path):
    db_file = tmp_path / "autocommit_ddl.aaydb"
    # Autocommit INSERT crash after commit fsync
    proc1 = _run_crash_subprocess(
        db_file,
        "after_commit_fsync",
        _baseline_setup(),
        ["AAYUSH INSERT INTO accounts VALUES (3, 'Neha', 750)"],
    )
    assert proc1.returncode == CRASH_EXIT_CODE

    # Crash after DDL + index creation inside explicit BEGIN/COMMIT
    proc2 = _run_crash_subprocess(
        db_file,
        "after_commit_fsync",
        [],
        [
            "AAYUSH BEGIN",
            "AAYUSH CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total INT)",
            "AAYUSH CREATE INDEX idx_orders_user ON orders (user_id)",
            "AAYUSH INSERT INTO orders VALUES (10, 3, 120), (11, 3, 80)",
            "AAYUSH DELETE FROM accounts WHERE id = 2",
            "AAYUSH COMMIT",
        ],
    )
    assert proc2.returncode == CRASH_EXIT_CODE

    with Engine(db_file) as e:
        assert _rows(e) == [
            {"id": 1, "owner": "Aayush", "balance": 1000},
            {"id": 3, "owner": "Neha", "balance": 750},
        ]
        assert "idx_orders_user" in e.db.indexes
        e.db.indexes["idx_orders_user"].validate()
        assert e.execute("AAYUSH SELECT id, total FROM orders WHERE user_id = 3 ORDER BY id ASC")["rows"] == [
            {"id": 10, "total": 120},
            {"id": 11, "total": 80},
        ]
        assert "INDEX SCAN: idx_orders_user" in e.execute(
            "AAYUSH EXPLAIN SELECT id FROM orders WHERE user_id = 3"
        )["message"]


# --- WAL Integrity, Corrupt Records, and Idempotency ---


def test_trailing_partial_and_checksum_corrupt_records_are_safely_ignored(tmp_path: Path):
    # Case A: trailing partial JSON record after valid committed txn
    db_partial = tmp_path / "partial.aaydb"
    with Engine(db_partial) as e:
        for s in _baseline_setup():
            e.execute(s)

    storage = JsonFileStorage(db_partial)
    db_state = storage.load()
    db_state.tables["accounts"].rows.append({"id": 3, "owner": "Mika", "balance": 250})
    storage.wal.append_commit_transaction(db_state.to_dict())
    wal_path = storage.wal.wal_path
    with open(wal_path, "ab") as f:
        f.write(b'{"lsn":4,"txn":"txn-torn","type":"begin"')  # missing closing brace & newline

    _verify_reopen_idempotency(
        db_partial,
        [
            {"id": 1, "owner": "Aayush", "balance": 1000},
            {"id": 2, "owner": "Alex", "balance": 500},
            {"id": 3, "owner": "Mika", "balance": 250},
        ],
    )

    # Case B: trailing checksum-corrupt record (valid JSON, wrong checksum) after committed txn
    db_bad_sum = tmp_path / "bad_checksum.aaydb"
    with Engine(db_bad_sum) as e:
        for s in _baseline_setup():
            e.execute(s)

    storage2 = JsonFileStorage(db_bad_sum)
    db_state2 = storage2.load()
    db_state2.tables["accounts"].rows.append({"id": 4, "owner": "Zara", "balance": 300})
    storage2.wal.append_commit_transaction(db_state2.to_dict())
    tampered = build_record(99, "txn-tampered", "begin")
    tampered["checksum"] = "0" * 64
    with open(storage2.wal.wal_path, "ab") as f:
        f.write(serialize_record(tampered))

    _verify_reopen_idempotency(
        db_bad_sum,
        [
            {"id": 1, "owner": "Aayush", "balance": 1000},
            {"id": 2, "owner": "Alex", "balance": 500},
            {"id": 4, "owner": "Zara", "balance": 300},
        ],
    )


def test_corrupt_record_before_later_valid_record_stops_and_never_skips_forward(tmp_path: Path):
    db_file = tmp_path / "mid_corrupt.aaydb"
    with Engine(db_file) as e:
        for s in _baseline_setup():
            e.execute(s)

    storage = JsonFileStorage(db_file)
    db_state = storage.load()
    base_snapshot_bytes = db_file.read_bytes()

    # Txn1 committed cleanly at LSN 1..3
    db_state.tables["accounts"].rows.append({"id": 3, "owner": "First", "balance": 100})
    commit_lsn, _ = storage.wal.append_commit_transaction(db_state.to_dict())

    # Insert a corrupted mid-WAL record BEFORE a later valid-looking Txn2
    wal_path = storage.wal.wal_path
    corrupt_mid = build_record(commit_lsn + 1, "txn-corrupt", "begin")
    corrupt_mid["checksum"] = "f" * 64

    db_state.tables["accounts"].rows.append({"id": 4, "owner": "LaterShouldNotApply", "balance": 999})
    txn2_begin = build_record(commit_lsn + 2, "txn-later", "begin")
    txn2_write = build_record(
        commit_lsn + 3,
        "txn-later",
        "write",
        image={**db_state.to_dict(), "wal_lsn": commit_lsn + 4},
    )
    txn2_commit = build_record(
        commit_lsn + 4, "txn-later", "commit", write_lsn=commit_lsn + 3
    )

    with open(wal_path, "ab") as f:
        f.write(serialize_record(corrupt_mid))
        f.write(serialize_record(txn2_begin))
        f.write(serialize_record(txn2_write))
        f.write(serialize_record(txn2_commit))

    with pytest.raises(StorageError, match="Corrupted WAL record"):
        parse_wal(wal_path, snapshot_wal_lsn=0)

    # Opening the database must stop at the corrupt record, raise StorageError,
    # and never apply the later transaction or overwrite the snapshot.
    with pytest.raises(StorageError, match="Corrupted WAL record"):
        Engine(db_file)
    assert db_file.read_bytes() == base_snapshot_bytes


def test_wal_rejects_non_monotonic_lsn_and_mismatched_txn_records(tmp_path: Path):
    wal_path = tmp_path / "bad_seq.wal"
    r1 = build_record(5, "txn-a", "begin")
    r2 = build_record(3, "txn-a", "write", image={})
    r3 = build_record(6, "txn-a", "commit", write_lsn=3)
    wal_path.write_bytes(serialize_record(r1) + serialize_record(r2) + serialize_record(r3))
    with pytest.raises(StorageError, match="non-monotonic LSN"):
        parse_wal(wal_path)

    # Cross-transaction association mismatch
    wal_mismatch = tmp_path / "mismatch.wal"
    a1 = build_record(1, "txn-a", "begin")
    b1 = build_record(2, "txn-b", "begin")
    a2 = build_record(3, "txn-a", "write", image={})
    c1 = build_record(4, "txn-b", "commit", write_lsn=3)  # txn-b had no write
    wal_mismatch.write_bytes(
        serialize_record(a1) + serialize_record(b1) + serialize_record(a2) + serialize_record(c1)
    )
    with pytest.raises(StorageError, match="COMMIT without matching WRITE"):
        parse_wal(wal_mismatch)


def test_multiple_committed_txns_and_trailing_uncommitted_in_one_wal(tmp_path: Path):
    db_file = tmp_path / "multi_wal.aaydb"
    with Engine(db_file) as e:
        for s in _baseline_setup():
            e.execute(s)

    storage = JsonFileStorage(db_file)
    db = storage.load()

    # Txn1 committed to WAL (without checkpointing)
    db.tables["accounts"].rows.append({"id": 3, "owner": "Mika", "balance": 250})
    storage.wal.append_commit_transaction(db.to_dict())

    # Txn2 committed to WAL (without checkpointing)
    db.tables["accounts"].rows[0]["balance"] = 850
    lsn2, _ = storage.wal.append_commit_transaction(db.to_dict())

    # Txn3 uncommitted (BEGIN + WRITE, no COMMIT)
    db.tables["accounts"].rows.append({"id": 99, "owner": "Uncommitted", "balance": 1})
    u_begin = build_record(lsn2 + 1, "txn-uncommitted", "begin")
    u_write = build_record(lsn2 + 2, "txn-uncommitted", "write", image=db.to_dict())
    with open(storage.wal.wal_path, "ab") as f:
        f.write(serialize_record(u_begin))
        f.write(serialize_record(u_write))

    _verify_reopen_idempotency(
        db_file,
        [
            {"id": 1, "owner": "Aayush", "balance": 850},
            {"id": 2, "owner": "Alex", "balance": 500},
            {"id": 3, "owner": "Mika", "balance": 250},
        ],
    )


def test_recovery_idempotency_on_identical_pre_recovery_state(tmp_path: Path):
    db_file = tmp_path / "idempotent.aaydb"
    with Engine(db_file) as e:
        for s in _baseline_setup():
            e.execute(s)

    storage = JsonFileStorage(db_file)
    db = storage.load()
    db.tables["accounts"].rows.append({"id": 3, "owner": "Mika", "balance": 250})
    commit_lsn, _ = storage.wal.append_commit_transaction(db.to_dict())
    # Also simulate checkpoint already written to .aaydb, WAL not truncated yet
    saved_wal = storage.wal.wal_path.read_bytes()

    with Engine(db_file) as r1:
        rows1 = _rows(r1)
        lsn1 = r1.db.wal_lsn
        assert lsn1 == commit_lsn

    # Restore identical WAL on top of already-checkpointed snapshot -> must NOT replay or duplicate
    storage.wal.wal_path.write_bytes(saved_wal)
    with Engine(db_file) as r2:
        assert _rows(r2) == rows1
        assert r2.db.wal_lsn == lsn1


# --- Commit error semantics (Case 1, Case 2, Case 3) ---


def test_checkpoint_failure_after_durable_commit_keeps_commit_and_preserves_wal(
    tmp_path: Path, monkeypatch
):
    db_file = tmp_path / "ckpt_fail.aaydb"
    engine = Engine(db_file)
    for s in _baseline_setup():
        engine.execute(s)

    def fail_checkpoint(*_args, **_kwargs):
        raise StorageError("snapshot write failed")

    monkeypatch.setattr(engine.storage, "_checkpoint", fail_checkpoint)

    # Transaction becomes durably committed in WAL; commit must NOT fail or roll back!
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'DurableInWal', 400)")
    res = engine.execute("AAYUSH COMMIT")
    assert res["message"] == "Transaction committed"
    assert not engine.in_transaction
    assert engine.storage.pending_checkpoint is True
    assert Path(f"{db_file}.wal").exists()
    assert len(_rows(engine)) == 3
    engine.close()
    assert Path(f"{db_file}.wal").exists()

    # Reopen recovers the transaction from WAL and completes checkpointing.
    _verify_reopen_idempotency(
        db_file,
        [
            {"id": 1, "owner": "Aayush", "balance": 1000},
            {"id": 2, "owner": "Alex", "balance": 500},
            {"id": 3, "owner": "DurableInWal", "balance": 400},
        ],
    )


def test_ambiguous_commit_fsync_failure_blocks_writes_and_requires_reopen(
    tmp_path: Path, monkeypatch
):
    db_file = tmp_path / "ambiguous.aaydb"
    engine = Engine(db_file)
    for s in _baseline_setup():
        engine.execute(s)

    real_fsync = os.fsync

    def fail_wal_fsync(fd: int) -> None:
        raise OSError("EIO during fsync")

    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'MaybeCommitted', 600)")
    monkeypatch.setattr("aaysql.wal.os.fsync", fail_wal_fsync)

    with pytest.raises(AmbiguousCommitError, match="ambiguous"):
        engine.execute("AAYUSH COMMIT")

    monkeypatch.setattr("aaysql.wal.os.fsync", real_fsync)
    assert engine.needs_recovery is True
    assert Path(f"{db_file}.wal").exists()

    # Subsequent operations are blocked until reopened.
    with pytest.raises(StorageError, match="reopen the database to recover"):
        engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Unsafe', 1)")
    with pytest.raises(StorageError, match="reopen the database to recover"):
        engine.execute("AAYUSH SELECT * FROM accounts")
    engine.close()

    # Reopening resolves the WAL deterministically (records were flushed before fsync failed).
    with Engine(db_file) as reopened:
        rows = _rows(reopened)
        assert len(rows) in (2, 3)


def test_multi_row_updates_deletes_and_rollback_with_indexes(tmp_path: Path):
    db_file = tmp_path / "multi_ops.aaydb"
    with Engine(db_file) as e:
        for s in _baseline_setup():
            e.execute(s)
        e.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Alex', 300), (4, 'Neha', 200)")

        # Multi-row update & delete inside transaction, then rollback
        e.execute("AAYUSH BEGIN")
        e.execute("AAYUSH UPDATE accounts SET balance = 0 WHERE owner = 'Alex'")
        e.execute("AAYUSH DELETE FROM accounts WHERE id = 4")
        e.execute("AAYUSH ROLLBACK")
        assert len(_rows(e)) == 4

        # Atomic batch failure inside transaction preserves earlier work
        e.execute("AAYUSH BEGIN")
        e.execute("AAYUSH UPDATE accounts SET balance = 999 WHERE owner = 'Alex'")
        with pytest.raises(ConstraintError):
            e.execute("AAYUSH UPDATE accounts SET id = 1")
        e.execute("AAYUSH COMMIT")

    with Engine(db_file) as reopened:
        assert reopened.execute(
            "AAYUSH SELECT id, balance FROM accounts WHERE owner = 'Alex' ORDER BY id ASC"
        )["rows"] == [{"id": 2, "balance": 999}, {"id": 3, "balance": 999}]


# --- PHASE 3: Format v3 & v1/v2 Migration ---


def test_v1_and_v2_migrations_preserve_data_on_write_and_never_overwrite_on_read(tmp_path: Path):
    # v1 fixture
    v1_file = tmp_path / "v1.aaydb"
    v1_payload = {
        "format": "aaysql",
        "format_version": 1,
        "engine_version": "0.3.0",
        "tables": {
            "users": {
                "name": "users",
                "columns": [
                    {
                        "name": "id",
                        "type_name": "INT",
                        "primary_key": True,
                        "not_null": False,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    },
                    {
                        "name": "name",
                        "type_name": "TEXT",
                        "primary_key": False,
                        "not_null": True,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    },
                ],
                "rows": [{"id": 1, "name": "Aayush"}],
            }
        },
    }
    v1_raw = json.dumps(v1_payload)
    v1_file.write_text(v1_raw, encoding="utf-8")

    with Engine(v1_file) as e1:
        assert e1.db.wal_lsn == 0
        assert e1.execute("AAYUSH SELECT * FROM users")["rows"] == [{"id": 1, "name": "Aayush"}]
    # Read-only open/SELECT must not modify legacy v1 file.
    assert v1_file.read_text(encoding="utf-8") == v1_raw

    with Engine(v1_file) as e1_write:
        e1_write.execute("AAYUSH INSERT INTO users VALUES (2, 'Alex')")
    migrated_v1 = json.loads(v1_file.read_text(encoding="utf-8"))
    assert migrated_v1["format_version"] == FORMAT_VERSION == 3
    assert migrated_v1["engine_version"] == ENGINE_VERSION == __version__ == "0.6.0"
    assert migrated_v1["wal_lsn"] > 0

    # v2 fixture with index
    v2_file = tmp_path / "v2.aaydb"
    v2_payload = {
        "format": "aaysql",
        "format_version": 2,
        "engine_version": "0.5.0",
        "tables": v1_payload["tables"],
        "indexes": {"idx_users_name": {"table": "users", "column": "name"}},
    }
    v2_raw = json.dumps(v2_payload)
    v2_file.write_text(v2_raw, encoding="utf-8")

    with Engine(v2_file) as e2:
        assert e2.db.wal_lsn == 0
        assert e2.execute("AAYUSH SELECT id FROM users WHERE name = 'Aayush'")["rows"] == [{"id": 1}]
    assert v2_file.read_text(encoding="utf-8") == v2_raw

    with Engine(v2_file) as e2_write:
        e2_write.execute("AAYUSH INSERT INTO users VALUES (2, 'Alex')")
    migrated_v2 = json.loads(v2_file.read_text(encoding="utf-8"))
    assert migrated_v2["format_version"] == 3
    assert migrated_v2["indexes"] == {"idx_users_name": {"table": "users", "column": "name"}}
    assert migrated_v2["wal_lsn"] > 0

    with Engine(v2_file) as e2_verify:
        assert e2_verify.execute("AAYUSH SELECT id FROM users WHERE name = 'Alex'")["rows"] == [{"id": 2}]


def test_v3_requires_valid_wal_lsn():
    base = {
        "format": "aaysql",
        "format_version": 3,
        "engine_version": "0.6.0",
        "tables": {},
        "indexes": {},
    }
    with pytest.raises(StorageError, match="wal_lsn"):
        Database.from_dict(base)
    with pytest.raises(StorageError, match="wal_lsn"):
        Database.from_dict({**base, "wal_lsn": -1})
    with pytest.raises(StorageError, match="wal_lsn"):
        Database.from_dict({**base, "wal_lsn": True})


def test_failed_commit_before_durable_wal_commit_restores_state(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "pre_commit_fail.aaydb"
    engine = Engine(db_file)
    for s in _baseline_setup():
        engine.execute(s)

    def fail_wal(_payload):
        raise StorageError("disk full before commit")

    monkeypatch.setattr(engine.storage.wal, "append_commit_transaction", fail_wal)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'NeverCommitted', 777)")
    with pytest.raises(StorageError, match="disk full before commit"):
        engine.execute("AAYUSH COMMIT")

    assert not engine.in_transaction
    assert not engine.needs_recovery
    assert _rows(engine) == [
        {"id": 1, "owner": "Aayush", "balance": 1000},
        {"id": 2, "owner": "Alex", "balance": 500},
    ]
    engine.close()

    with Engine(db_file) as reopened:
        assert len(_rows(reopened)) == 2


def test_subprocess_crash_index_create_before_commit_and_drop_ddl_after_commit(tmp_path: Path):
    db_file = tmp_path / "index_drop_crash.aaydb"
    # Create second index inside transaction, crash before COMMIT -> index must NOT exist.
    proc1 = _run_crash_subprocess(
        db_file,
        "after_write_before_commit",
        _baseline_setup(),
        [
            "AAYUSH BEGIN",
            "AAYUSH CREATE INDEX idx_accounts_balance ON accounts (balance)",
            "AAYUSH COMMIT",
        ],
    )
    assert proc1.returncode == CRASH_EXIT_CODE
    with Engine(db_file) as e1:
        assert "idx_accounts_balance" not in e1.db.indexes
        assert "idx_accounts_owner" in e1.db.indexes

    # Autocommit UPDATE & DELETE + DROP INDEX crash after COMMIT fsync -> all recovered.
    proc2 = _run_crash_subprocess(
        db_file,
        "after_commit_fsync",
        [],
        [
            "AAYUSH UPDATE accounts SET balance = 1234 WHERE id = 1",
        ],
    )
    assert proc2.returncode == CRASH_EXIT_CODE

    proc3 = _run_crash_subprocess(
        db_file,
        "after_commit_fsync",
        [],
        [
            "AAYUSH BEGIN",
            "AAYUSH DELETE FROM accounts WHERE id = 2",
            "AAYUSH DROP INDEX idx_accounts_owner",
            "AAYUSH COMMIT",
        ],
    )
    assert proc3.returncode == CRASH_EXIT_CODE

    with Engine(db_file) as e2:
        assert _rows(e2) == [{"id": 1, "owner": "Aayush", "balance": 1234}]
        assert "idx_accounts_owner" not in e2.db.indexes

