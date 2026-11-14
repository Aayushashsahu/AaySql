"""Default READ COMMITTED and explicit SNAPSHOT isolation semantics."""

from __future__ import annotations

from aaysql import Engine


def _rows(session, sql):
    return session.execute(sql)["rows"]


def test_read_committed_sees_nonrepeatable_read_between_statements():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 10)")
    reader = engine.connect()  # READ COMMITTED is the server/session default
    writer = engine.connect()
    reader.execute("AAYUSH BEGIN")
    assert _rows(reader, "AAYUSH SELECT v FROM t WHERE id = 1") == [{"v": 10}]
    writer.execute("AAYUSH UPDATE t SET v = 20 WHERE id = 1")
    assert _rows(reader, "AAYUSH SELECT v FROM t WHERE id = 1") == [{"v": 20}]
    reader.execute("AAYUSH COMMIT")


def test_snapshot_repeats_the_same_read_and_hides_phantoms():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 10)")
    reader = engine.connect("SNAPSHOT")
    writer = engine.connect()
    reader.execute("AAYUSH BEGIN SNAPSHOT")
    first = _rows(reader, "AAYUSH SELECT id, v FROM t ORDER BY id")
    writer.execute("AAYUSH UPDATE t SET v = 20 WHERE id = 1")
    writer.execute("AAYUSH INSERT INTO t VALUES (2, 30)")
    second = _rows(reader, "AAYUSH SELECT id, v FROM t ORDER BY id")
    assert first == second == [{"id": 1, "v": 10}]
    reader.execute("AAYUSH COMMIT")
    assert _rows(reader, "AAYUSH SELECT id, v FROM t ORDER BY id") == [
        {"id": 1, "v": 20},
        {"id": 2, "v": 30},
    ]


def test_snapshot_writer_conflicts_but_read_only_snapshot_does_not_block():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 0)")
    snapshot = engine.connect("SNAPSHOT")
    writer = engine.connect()
    snapshot.execute("AAYUSH BEGIN SNAPSHOT")
    writer.execute("AAYUSH UPDATE t SET v = 1 WHERE id = 1")
    # A read-only snapshot keeps reading its old version without conflicts.
    assert _rows(snapshot, "AAYUSH SELECT v FROM t") == [{"v": 0}]
    snapshot.execute("AAYUSH COMMIT")
