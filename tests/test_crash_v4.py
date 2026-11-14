"""Format v4 crash boundaries: commit durability and checkpoint recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.errors import StorageError
from aaysql.storage import SegmentedStorage

SRC_DIR = str(Path(__file__).parents[1] / "src")
CRASH_EXIT_CODE = 99

CHILD = """
import json
import os
import sys

db_path = sys.argv[1]
crash_point = sys.argv[2]
setup_sql = json.loads(sys.argv[3])
target_sql = json.loads(sys.argv[4])

from aaysql import Engine

engine = Engine(db_path)
for statement in setup_sql:
    engine.execute(statement)

os.environ["_AAYSQL_TEST_CRASH_POINT"] = crash_point
for statement in target_sql:
    if statement == "CHECKPOINT":
        engine.checkpoint()
    else:
        engine.execute(statement)

raise AssertionError(f"expected exit at {crash_point!r}")
"""


def _run_crash(
    db_path: Path,
    crash_point: str,
    *,
    setup: list[str],
    target: list[str],
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("_AAYSQL_TEST_CRASH_POINT", None)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD,
            str(db_path),
            crash_point,
            json.dumps(setup),
            json.dumps(target),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


SETUP = ["AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v TEXT)"]
INSERT = "AAYUSH INSERT INTO t VALUES (1, 'committed')"


def _ids(db_path: Path) -> list[int]:
    with Engine(db_path) as engine:
        return [row["id"] for row in engine.execute("AAYUSH SELECT id FROM t ORDER BY id ASC")["rows"]]


def _assert_exit(result: subprocess.CompletedProcess, point: str) -> None:
    assert result.returncode == CRASH_EXIT_CODE, (point, result.returncode, result.stderr)


# --- before the commit point -----------------------------------------------


@pytest.mark.parametrize(
    "point", ["after_begin", "after_write_before_commit", "partial_commit_record"]
)
def test_crash_before_commit_leaves_no_trace(tmp_path: Path, point: str):
    db_path = tmp_path / f"{point}.aaydb"
    result = _run_crash(db_path, point, setup=SETUP, target=[INSERT])

    _assert_exit(result, point)
    # The aborted transaction stays in the WAL and is discarded by recovery.
    assert Path(f"{db_path}.wal").stat().st_size > 0
    assert _ids(db_path) == []


# --- at or after the commit point ------------------------------------------


def test_crash_after_commit_fsync_is_recovered(tmp_path: Path):
    db_path = tmp_path / "after_commit.aaydb"
    result = _run_crash(db_path, "after_commit_fsync", setup=SETUP, target=[INSERT])

    _assert_exit(result, "after_commit_fsync")
    assert _ids(db_path) == [1]
    # Recovery checkpointed, so the WAL is consumed exactly once.
    assert _ids(db_path) == [1]
    assert not Path(f"{db_path}.wal").exists() or Path(f"{db_path}.wal").stat().st_size == 0


def test_crash_after_checkpoint_before_truncate_does_not_duplicate(tmp_path: Path):
    db_path = tmp_path / "after_checkpoint.aaydb"
    result = _run_crash(
        db_path,
        "after_checkpoint_before_truncate",
        setup=SETUP,
        target=[INSERT, "CHECKPOINT"],
    )

    _assert_exit(result, "after_checkpoint_before_truncate")
    assert _ids(db_path) == [1]
    assert _ids(db_path) == [1]


def test_crash_during_wal_truncation_does_not_duplicate(tmp_path: Path):
    db_path = tmp_path / "during_truncate.aaydb"
    result = _run_crash(
        db_path,
        "during_wal_truncation",
        setup=SETUP,
        target=[INSERT, "CHECKPOINT"],
    )

    _assert_exit(result, "during_wal_truncation")
    assert _ids(db_path) == [1]
    assert _ids(db_path) == [1]


# --- inside a checkpoint ----------------------------------------------------


def test_crash_after_segment_write_before_replace_keeps_the_commit(tmp_path: Path):
    db_path = tmp_path / "segment_before_replace.aaydb"
    result = _run_crash(
        db_path,
        "after_segment_write_before_replace",
        setup=SETUP,
        target=[INSERT, "CHECKPOINT"],
    )

    _assert_exit(result, "after_segment_write_before_replace")
    assert _ids(db_path) == [1]


def test_crash_after_segment_fsync_keeps_the_commit(tmp_path: Path):
    db_path = tmp_path / "segment_fsync.aaydb"
    result = _run_crash(
        db_path,
        "after_segment_fsync",
        setup=SETUP,
        target=[INSERT, "CHECKPOINT"],
    )

    _assert_exit(result, "after_segment_fsync")
    assert _ids(db_path) == [1]


def test_crash_before_manifest_replace_keeps_the_commit(tmp_path: Path):
    db_path = tmp_path / "manifest_before_replace.aaydb"
    result = _run_crash(
        db_path,
        "after_manifest_write_before_replace",
        setup=SETUP,
        target=[INSERT, "CHECKPOINT"],
    )

    _assert_exit(result, "after_manifest_write_before_replace")
    assert _ids(db_path) == [1]


def test_crash_after_manifest_replace_keeps_the_commit(tmp_path: Path):
    db_path = tmp_path / "manifest_replace.aaydb"
    result = _run_crash(
        db_path,
        "after_manifest_replace",
        setup=SETUP,
        target=[INSERT, "CHECKPOINT"],
    )

    _assert_exit(result, "after_manifest_replace")
    assert _ids(db_path) == [1]


# --- checkpoint failure is not a rollback ----------------------------------


def test_failed_checkpoint_after_commit_keeps_the_delta_in_the_wal(
    tmp_path: Path, monkeypatch
):
    from aaysql.delta import apply_changes

    db_path = tmp_path / "pending.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")

    storage = SegmentedStorage(db_path, checkpoint_bytes=1)
    db = storage.load()
    changes = [{"t": "insert", "table": "t", "rows": [{"id": 1}]}]
    apply_changes(db, changes)

    def boom(*args, **kwargs):
        raise StorageError("simulated manifest failure")

    monkeypatch.setattr("aaysql.segments.publish_manifest", boom)
    storage.save(db, changes)

    assert storage.pending_checkpoint is True
    assert isinstance(storage.last_checkpoint_error, StorageError)
    assert Path(f"{db_path}.wal").stat().st_size > 0

    monkeypatch.undo()
    assert _ids(db_path) == [1]


def test_explicit_checkpoint_failure_raises_but_keeps_the_delta(
    tmp_path: Path, monkeypatch
):
    db_path = tmp_path / "explicit_fail.aaydb"
    engine = Engine(db_path)
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")

    def boom(*args, **kwargs):
        raise StorageError("simulated manifest failure")

    monkeypatch.setattr("aaysql.segments.publish_manifest", boom)
    engine.execute("AAYUSH INSERT INTO t VALUES (1)")

    with pytest.raises(StorageError, match="simulated manifest failure"):
        engine.checkpoint()
    assert [row["id"] for row in engine.execute("AAYUSH SELECT id FROM t")["rows"]] == [1]

    engine.close()
    monkeypatch.undo()
    assert _ids(db_path) == [1]


# --- recovery itself crashing ----------------------------------------------


def _append_committed_delta_and_torn_tail(db_path: Path) -> None:
    from aaysql import segments
    from aaysql.wal import WalManager

    manifest = segments.read_manifest(db_path)
    manager = WalManager(db_path)
    manager.last_lsn = manifest["checkpoint_lsn"]
    manager.append_delta_transaction(
        [{"t": "insert", "table": "t", "rows": [{"id": 7}]}],
        db_id=manifest["db_id"],
    )
    with open(Path(f"{db_path}.wal"), "ab") as handle:
        handle.write(b'{"lsn":999,"txn":"torn","type":"beg')


def test_crash_during_tail_repair_recovers_the_commit_once(tmp_path: Path):
    db_path = tmp_path / "tail_repair.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    _append_committed_delta_and_torn_tail(db_path)

    script = """
import os
import sys

os.environ["_AAYSQL_TEST_CRASH_POINT"] = sys.argv[2]
from aaysql import Engine

Engine(sys.argv[1]).close()
raise AssertionError("expected exit during tail repair")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC_DIR + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("_AAYSQL_TEST_CRASH_POINT", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(db_path), "after_tail_repair"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == CRASH_EXIT_CODE, result.stderr

    assert _ids(db_path) == [7]
    assert _ids(db_path) == [7]
