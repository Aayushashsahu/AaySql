"""Format v4 delta WAL: capture, framing, and replay."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine, segments
from aaysql.delta import apply_changes
from aaysql.errors import StorageError
from aaysql.storage import SegmentedStorage
from aaysql.wal import WalManager, build_record_v4, parse_wal_v4, serialize_record


def _records(db_path: Path) -> list[dict]:
    text = Path(f"{db_path}.wal").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _writes(db_path: Path) -> list[dict]:
    return [rec for rec in _records(db_path) if rec["type"] == "write"]


def _manifest(db_path: Path) -> dict:
    return segments.read_manifest(db_path)


def _append_delta(db_path: Path, changes: list[dict]) -> int:
    manifest = _manifest(db_path)
    wal_path = Path(f"{db_path}.wal")
    max_lsn = manifest["checkpoint_lsn"]
    if wal_path.exists() and wal_path.stat().st_size > 0:
        max_lsn = max(
            max_lsn,
            parse_wal_v4(
                wal_path,
                manifest["db_id"],
                checkpoint_lsn=manifest["checkpoint_lsn"],
            ).max_lsn,
        )
    manager = WalManager(db_path)
    manager.last_lsn = max_lsn
    commit_lsn, _ = manager.append_delta_transaction(
        changes, db_id=manifest["db_id"]
    )
    return commit_lsn


# --- capture ---------------------------------------------------------------


def test_autocommit_insert_writes_a_delta_not_a_database_image(tmp_path: Path):
    db_path = tmp_path / "delta.aaydb"
    engine = Engine(db_path)
    engine.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, email TEXT)")
    engine.execute("AAYUSH INSERT INTO users VALUES (1, 'a@x'), (2, 'b@x')")

    manifest = _manifest(db_path)
    writes = _writes(db_path)
    inserts = [w for w in writes if w["changes"][0]["t"] == "insert"]
    assert len(inserts) == 1
    write = inserts[0]
    assert "image" not in write
    assert write["v"] == 4
    assert write["db"] == manifest["db_id"]
    assert write["changes"] == [
        {
            "t": "insert",
            "table": "users",
            "rows": [
                {"id": 1, "email": "a@x"},
                {"id": 2, "email": "b@x"},
            ],
        }
    ]
    # The WAL grows with the change, not with the whole database.
    assert len(json.dumps(write)) < 400
    engine.close()


def test_update_and_delete_use_row_positions_and_delete_descends(tmp_path: Path):
    db_path = tmp_path / "positions.aaydb"
    engine = Engine(db_path)
    engine.execute("AAYUSH CREATE TABLE t (id INT, v TEXT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    engine.execute("AAYUSH UPDATE t SET v = 'z' WHERE id = 2")
    engine.execute("AAYUSH DELETE FROM t WHERE id >= 1")

    writes = _writes(db_path)
    changes = [change for write in writes for change in write["changes"]]
    updates = [c for c in changes if c["t"] == "update"]
    deletes = [c for c in changes if c["t"] == "delete"]
    assert updates == [
        {"t": "update", "table": "t", "index": 1, "row": {"id": 2, "v": "z"}}
    ]
    # DELETE removes indexes 2, 1, 0 in descending order.
    assert [c["index"] for c in deletes] == [2, 1, 0]
    engine.close()


def test_ddl_is_captured_as_delta_changes(tmp_path: Path):
    db_path = tmp_path / "ddl.aaydb"
    engine = Engine(db_path)
    engine.execute("AAYUSH CREATE TABLE t (id INT)")
    engine.execute("AAYUSH CREATE INDEX idx_t_id ON t (id)")
    engine.execute("AAYUSH DROP INDEX idx_t_id")
    engine.execute("AAYUSH DROP TABLE t")

    changes = [c for write in _writes(db_path) for c in write["changes"]]
    engine.close()
    kinds = [c["t"] for c in changes]
    assert kinds == ["create_table", "create_index", "drop_index", "drop_table"]
    assert changes[0]["columns"][0]["name"] == "id"


# --- replay ----------------------------------------------------------------


def test_replay_reproduces_order_duplicates_and_mixed_operations(tmp_path: Path):
    db_path = tmp_path / "replay.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT, v TEXT)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1, 'a'), (2, 'b'), (2, 'b')")
    _append_delta(
        db_path,
        [
            {"t": "insert", "table": "t", "rows": [{"id": 3, "v": "c"}]},
            {"t": "update", "table": "t", "index": 0, "row": {"id": 1, "v": "A"}},
            {"t": "delete", "table": "t", "index": 2},
            {"t": "update", "table": "t", "index": 1, "row": {"id": 2, "v": "B"}},
            {"t": "delete", "table": "t", "index": 0},
            {"t": "delete", "table": "t", "index": 0},
        ],
    )

    # insert 3c -> [1a,2b,2b,3c]; update 0 -> [1A,2b,2b,3c]; delete 2 ->
    # [1A,2b,3c]; update 1 -> [1A,2B,3c]; delete 0 -> [2B,3c]; delete 0 -> [3c].
    expected = [{"id": 3, "v": "c"}]
    with Engine(db_path) as recovered:
        assert recovered.execute("AAYUSH SELECT id, v FROM t")["rows"] == expected
    # Recovery checkpointed, so a second open must be identical (idempotent).
    with Engine(db_path) as recovered_again:
        assert recovered_again.execute("AAYUSH SELECT id, v FROM t")["rows"] == expected


def test_replay_of_ddl_rebuilds_catalog_and_index_trees(tmp_path: Path):
    db_path = tmp_path / "replay_ddl.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE base (id INT)")
    _append_delta(
        db_path,
        [
            {
                "t": "create_table",
                "table": "logs",
                "columns": [
                    {
                        "name": "id",
                        "type_name": "INT",
                        "primary_key": True,
                        "not_null": True,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    },
                    {
                        "name": "msg",
                        "type_name": "TEXT",
                        "primary_key": False,
                        "not_null": False,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    },
                ],
            },
            {"t": "insert", "table": "logs", "rows": [{"id": 1, "msg": "x"}]},
            {
                "t": "create_index",
                "index": "idx_logs_msg",
                "table": "logs",
                "column": "msg",
            },
        ],
    )
    with Engine(db_path) as recovered:
        assert recovered.db.indexes["idx_logs_msg"].lookup("x")[0]["id"] == 1
        plan = recovered.execute(
            "AAYUSH EXPLAIN SELECT id FROM logs WHERE msg = 'x'"
        )["message"]
        assert "INDEX SCAN: idx_logs_msg" in plan


def test_apply_changes_rejects_out_of_range_positions(tmp_path: Path):
    db_path = tmp_path / "range.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
    storage = SegmentedStorage(db_path)
    db = storage.load()
    with pytest.raises(StorageError, match="out of range"):
        apply_changes(
            db, [{"t": "delete", "table": "t", "index": 5}]
        )


# --- framing and corruption -------------------------------------------------


def test_torn_tail_is_ignored_and_repaired(tmp_path: Path):
    db_path = tmp_path / "torn.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1)")
    wal_path = Path(f"{db_path}.wal")
    with open(wal_path, "ab") as handle:
        handle.write(b'{"lsn":999,"txn":"torn","type":"beg')

    with Engine(db_path) as recovered:
        assert recovered.execute("AAYUSH SELECT id FROM t")["rows"] == [{"id": 1}]
    # The torn tail was discarded; a clean close removes the empty WAL.
    wal_path = Path(f"{db_path}.wal")
    assert not wal_path.exists() or wal_path.stat().st_size == 0


def test_mid_log_corruption_fails_closed(tmp_path: Path):
    db_path = tmp_path / "midlog.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
    _append_delta(db_path, [{"t": "insert", "table": "t", "rows": [{"id": 1}]}])
    _append_delta(db_path, [{"t": "insert", "table": "t", "rows": [{"id": 2}]}])
    wal_path = Path(f"{db_path}.wal")
    lines = wal_path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) >= 6
    corrupt = lines[0][:-2] + ("x" if lines[0][-2] != "x" else "y") + lines[0][-1]
    wal_path.write_text(corrupt + "".join(lines[1:]), encoding="utf-8")

    with pytest.raises(StorageError, match="Corrupted WAL"):
        Engine(db_path)


def test_foreign_database_id_is_rejected(tmp_path: Path):
    db_path = tmp_path / "foreign.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
    wal_path = Path(f"{db_path}.wal")
    record = build_record_v4(
        999,
        "txn-foreign",
        "begin",
        db_id="f" * 32,
    )
    with open(wal_path, "ab") as handle:
        handle.write(serialize_record(record))

    with pytest.raises(StorageError, match="different database"):
        Engine(db_path)


def test_parse_wal_v4_marks_already_checkpointed_transactions(tmp_path: Path):
    db_path = tmp_path / "lsn.aaydb"
    with Engine(db_path) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT)")
    storage = SegmentedStorage(db_path)
    storage.load()
    commit_lsn = _append_delta(
        db_path, [{"t": "insert", "table": "t", "rows": [{"id": 1}]}]
    )
    analysis = parse_wal_v4(
        Path(f"{db_path}.wal"), storage.db_id or "", checkpoint_lsn=commit_lsn
    )
    assert analysis.already_checkpointed == 1
    assert analysis.committed[0].commit_lsn == commit_lsn
