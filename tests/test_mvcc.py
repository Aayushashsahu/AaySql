"""Snapshot-consistent readers and copy-on-write publication (Phase 4)."""

from __future__ import annotations

import threading

import pytest

from aaysql import Engine
from aaysql.errors import ConstraintError, ExecutionError, WriteConflictError


def _rows(session, sql):
    return session.execute(sql)["rows"]


def test_reader_sees_complete_committed_version_only():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 'old')")
    reader = engine.connect("SNAPSHOT")
    writer = engine.connect()

    reader.execute("AAYUSH BEGIN")
    assert _rows(reader, "AAYUSH SELECT * FROM t") == [{"id": 1, "v": "old"}]

    writer.execute("AAYUSH BEGIN")
    writer.execute("AAYUSH INSERT INTO t VALUES (2, 'new')")
    writer.execute("AAYUSH UPDATE t SET v = 'new' WHERE id = 1")
    assert _rows(writer, "AAYUSH SELECT * FROM t") == [
        {"id": 1, "v": "new"},
        {"id": 2, "v": "new"},
    ]
    # The pinned snapshot cannot see either half of the writer's workspace.
    assert _rows(reader, "AAYUSH SELECT * FROM t") == [{"id": 1, "v": "old"}]
    writer.execute("AAYUSH COMMIT")
    assert _rows(reader, "AAYUSH SELECT * FROM t") == [{"id": 1, "v": "old"}]

    reader.execute("AAYUSH COMMIT")
    assert _rows(reader, "AAYUSH SELECT * FROM t") == [
        {"id": 1, "v": "new"},
        {"id": 2, "v": "new"},
    ]


def test_read_committed_refreshes_each_statement():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    reader = engine.connect()
    writer = engine.connect()
    reader.execute("AAYUSH BEGIN")
    assert _rows(reader, "AAYUSH SELECT * FROM t") == []
    writer.execute("AAYUSH INSERT INTO t VALUES (1)")
    # READ COMMITTED is the default: the next statement sees the new commit.
    assert _rows(reader, "AAYUSH SELECT * FROM t") == [{"id": 1}]
    reader.execute("AAYUSH ROLLBACK")


def test_uncommitted_and_rolled_back_workspaces_are_never_published():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    writer = engine.connect()
    reader = engine.connect("SNAPSHOT")
    reader.execute("AAYUSH BEGIN")
    writer.execute("AAYUSH BEGIN")
    writer.execute("AAYUSH INSERT INTO t VALUES (1)")
    writer.execute("AAYUSH ROLLBACK")
    assert _rows(reader, "AAYUSH SELECT * FROM t") == []
    assert engine.execute("AAYUSH SELECT * FROM t")["rows"] == []


def test_snapshot_visibility_of_index_and_table_scans_match():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, grp INT)")
    engine.execute("AAYUSH CREATE INDEX idx_grp ON t (grp)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 1), (2, 2), (3, 1)")
    reader = engine.connect("SNAPSHOT")
    writer = engine.connect()
    reader.execute("AAYUSH BEGIN")

    writer.execute("AAYUSH DELETE FROM t WHERE id = 2")
    writer.execute("AAYUSH UPDATE t SET grp = 2 WHERE id = 3")
    writer.execute("AAYUSH INSERT INTO t VALUES (4, 1)")

    table_scan = _rows(
        reader, "AAYUSH SELECT id, grp FROM t WHERE grp = 1 ORDER BY id"
    )
    index_scan = _rows(
        reader,
        "AAYUSH SELECT id, grp FROM t WHERE grp = 1 ORDER BY id",
    )
    assert table_scan == [{"id": 1, "grp": 1}, {"id": 3, "grp": 1}]
    assert index_scan == table_scan

    reader.execute("AAYUSH COMMIT")
    assert _rows(reader, "AAYUSH SELECT id, grp FROM t WHERE grp = 1 ORDER BY id") == [
        {"id": 1, "grp": 1},
        {"id": 4, "grp": 1},
    ]


def test_snapshot_does_not_see_later_ddl():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    reader = engine.connect("SNAPSHOT")
    writer = engine.connect()
    reader.execute("AAYUSH BEGIN")
    writer.execute("AAYUSH CREATE TABLE later (id INT PRIMARY KEY)")
    with pytest.raises(ExecutionError):
        reader.execute("AAYUSH SELECT * FROM later")
    reader.execute("AAYUSH COMMIT")
    assert reader.execute("AAYUSH SELECT * FROM later")["rows"] == []


def test_first_updater_wins_on_same_table():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 0)")
    first = engine.connect()
    second = engine.connect()
    first.execute("AAYUSH BEGIN")
    second.execute("AAYUSH BEGIN")
    first.execute("AAYUSH UPDATE t SET v = 1 WHERE id = 1")
    second.execute("AAYUSH UPDATE t SET v = 2 WHERE id = 1")
    first.execute("AAYUSH COMMIT")
    with pytest.raises(WriteConflictError) as excinfo:
        second.execute("AAYUSH COMMIT")
    assert excinfo.value.code == "WRITE_CONFLICT"
    assert engine.execute("AAYUSH SELECT v FROM t")["rows"] == [{"v": 1}]
    assert not second.in_transaction


def test_disjoint_inserts_do_not_conflict_and_keep_unique_keys():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    first = engine.connect()
    second = engine.connect()
    first.execute("AAYUSH BEGIN")
    second.execute("AAYUSH BEGIN")
    first.execute("AAYUSH INSERT INTO t VALUES (1)")
    second.execute("AAYUSH INSERT INTO t VALUES (2)")
    first.execute("AAYUSH COMMIT")
    second.execute("AAYUSH COMMIT")
    assert engine.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 2}]

    third = engine.connect()
    fourth = engine.connect()
    third.execute("AAYUSH BEGIN")
    fourth.execute("AAYUSH BEGIN")
    third.execute("AAYUSH INSERT INTO t VALUES (3)")
    fourth.execute("AAYUSH INSERT INTO t VALUES (3)")
    third.execute("AAYUSH COMMIT")
    with pytest.raises(ConstraintError):
        fourth.execute("AAYUSH COMMIT")


def test_snapshot_release_is_deterministic_and_publishes_no_leaks():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    sessions = [engine.connect("SNAPSHOT") for _ in range(8)]
    for session in sessions:
        session.execute("AAYUSH BEGIN")
        session.execute("AAYUSH SELECT * FROM t")
    assert engine.mvcc.snapshot_stats()["retained"] == 8
    for session in sessions:
        session.execute("AAYUSH COMMIT")
    assert engine.mvcc.snapshot_stats()["retained"] == 0
    engine.execute("AAYUSH INSERT INTO t VALUES (1)")
    assert engine.mvcc.metrics()["retained_snapshots"] == 0


def test_snapshot_isolation_keyword_is_accepted():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    session = engine.connect()
    session.execute("AAYUSH BEGIN ISOLATION LEVEL SNAPSHOT")
    assert session.isolation.value == "SNAPSHOT"
    session.execute("AAYUSH ROLLBACK")


def test_concurrent_pinned_readers_never_mix_versions():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, batch INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (0, 0)")
    failures: list[BaseException] = []
    barrier = threading.Barrier(6)

    def reader(worker: int):
        try:
            session = engine.connect("SNAPSHOT")
            session.execute("AAYUSH BEGIN")
            before = _rows(session, "AAYUSH SELECT batch FROM t ORDER BY id")
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
            after = _rows(session, "AAYUSH SELECT batch FROM t ORDER BY id")
            if before != after:
                raise AssertionError(f"reader {worker} saw {before} then {after}")
            session.execute("AAYUSH COMMIT")
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for batch in range(1, 4):
        engine.execute(
            "AAYUSH INSERT INTO t VALUES "
            + ", ".join(f"({batch * 10 + i}, {batch})" for i in range(5))
        )
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=20)
    assert failures == []
