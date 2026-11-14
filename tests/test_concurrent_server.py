"""Concurrent server sessions: readers do not block, conflicts are stable."""

from __future__ import annotations

from pathlib import Path

import pytest

from aaysql.client import AayushSQLClient, ServerError
from aaysql.protocol import WRITE_CONFLICT
from aaysql.server import AayushSQLServer, ServerConfig


@pytest.fixture()
def server(tmp_path: Path):
    instance = AayushSQLServer(
        ServerConfig(
            db_path=tmp_path / "concurrent.aaydb",
            port=0,
            read_timeout=10.0,
            write_timeout=10.0,
            idle_timeout=30.0,
            shutdown_timeout=10.0,
        )
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.shutdown()


def _client(server, **kwargs):
    return AayushSQLClient(*server.address, **kwargs).connect()


def test_reader_continues_while_writer_transaction_is_open(server):
    writer = _client(server)
    reader = _client(server)
    try:
        writer.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        writer.execute("AAYUSH INSERT INTO t VALUES (1, 10)")
        writer.execute("AAYUSH BEGIN")
        writer.execute("AAYUSH UPDATE t SET v = 99 WHERE id = 1")
        writer.execute("AAYUSH INSERT INTO t VALUES (2, 20)")
        # The reader is not blocked and never observes the uncommitted row.
        assert reader.execute("AAYUSH SELECT * FROM t")["rows"] == [
            {"id": 1, "v": 10}
        ]
        writer.execute("AAYUSH COMMIT")
        assert reader.execute("AAYUSH SELECT * FROM t")["rows"] == [
            {"id": 1, "v": 99},
            {"id": 2, "v": 20},
        ]
    finally:
        writer.close()
        reader.close()


def test_two_clients_have_independent_transactions(server):
    first = _client(server)
    second = _client(server)
    try:
        first.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        first.execute("AAYUSH BEGIN")
        first.execute("AAYUSH INSERT INTO t VALUES (1)")
        second.execute("AAYUSH BEGIN")
        second.execute("AAYUSH INSERT INTO t VALUES (2)")
        second.execute("AAYUSH COMMIT")
        # First still sees its own snapshot, not second's committed row.
        assert first.execute("AAYUSH SELECT * FROM t")["rows"] == [{"id": 1}]
        first.execute("AAYUSH COMMIT")
        assert first.execute("AAYUSH SELECT * FROM t ORDER BY id")["rows"] == [
            {"id": 1},
            {"id": 2},
        ]
    finally:
        first.close()
        second.close()


def test_stale_writer_gets_write_conflict_code(server):
    first = _client(server)
    second = _client(server)
    try:
        first.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        first.execute("AAYUSH INSERT INTO t VALUES (1, 0)")
        first.execute("AAYUSH BEGIN")
        second.execute("AAYUSH BEGIN")
        first.execute("AAYUSH UPDATE t SET v = 1 WHERE id = 1")
        second.execute("AAYUSH UPDATE t SET v = 2 WHERE id = 1")
        first.execute("AAYUSH COMMIT")
        with pytest.raises(ServerError) as excinfo:
            second.execute("AAYUSH COMMIT")
        assert excinfo.value.code == WRITE_CONFLICT
        assert excinfo.value.retryable is True
        assert first.execute("AAYUSH SELECT v FROM t")["rows"] == [{"v": 1}]
    finally:
        first.close()
        second.close()


def test_snapshot_begin_is_visible_to_the_protocol(server):
    with _client(server) as client:
        client.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        client.execute("AAYUSH BEGIN")
        client.execute("AAYUSH INSERT INTO t VALUES (1)")
        client.execute("AAYUSH COMMIT")
