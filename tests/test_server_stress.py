"""Phase 5 server reliability and multi-client TCP stress tests.

Exercises repeated connect/disconnect, idle and slow-client timeouts, abrupt
disconnect during response transfer, and mixed multi-client workloads across
1, 2, 4, 8, 16, and 32 real TCP connections.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql.client import AayushSQLClient, ConnectionLostError, ServerError
from aaysql.protocol import CONNECTION_TIMEOUT, PROTOCOL_VERSION, TRANSACTION_BUSY, encode_message, frame_bytes
from aaysql.server import AayushSQLServer, ServerConfig


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- Phase 5.1: Connection lifecycle --------------------------------------


def test_repeated_connect_disconnect_releases_sessions_cleanly(tmp_path: Path):
    db_path = tmp_path / "churn.aaydb"
    server = AayushSQLServer(
        ServerConfig(db_path=db_path, port=0, max_clients=4, read_timeout=5.0, shutdown_timeout=5.0)
    )
    server.start()
    try:
        for i in range(25):
            with AayushSQLClient(*server.address, read_timeout=5.0) as client:
                assert client.ping()["pong"] is True
                if i == 0:
                    client.execute("AAYUSH CREATE TABLE churn (id INT PRIMARY KEY)")
                client.execute(f"AAYUSH INSERT INTO churn VALUES ({i})")
        assert _wait_for(lambda: server.session_count == 0)
        with AayushSQLClient(*server.address) as verify:
            assert verify.execute("AAYUSH SELECT COUNT(*) AS n FROM churn")["rows"] == [{"n": 25}]
    finally:
        assert server.shutdown() is True


def test_idle_timeout_closes_inactive_client():
    server = AayushSQLServer(
        ServerConfig(db_path=None, port=0, idle_timeout=0.25, read_timeout=0.2, shutdown_timeout=5.0)
    )
    server.start()
    try:
        client = AayushSQLClient(*server.address, read_timeout=2.0).connect()
        try:
            assert client.ping()["pong"] is True
            assert _wait_for(lambda: server.session_count == 0, timeout=3.0)
            with pytest.raises((ConnectionLostError, ServerError, OSError)):
                client.ping()
        finally:
            client.close()
    finally:
        server.shutdown()


def test_slow_client_partial_frame_times_out_without_blocking_others():
    server = AayushSQLServer(
        ServerConfig(db_path=None, port=0, read_timeout=0.25, idle_timeout=5.0, shutdown_timeout=5.0)
    )
    server.start()
    try:
        slow = socket.create_connection(server.address, timeout=3.0)
        try:
            # Complete handshake, then stall mid-header.
            hello = frame_bytes(
                encode_message(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "slow-hello",
                        "operation": "hello",
                        "client_name": "slow",
                        "client_version": "1.0.0",
                    }
                )
            )
            slow.sendall(hello)
            # Drain hello response
            hdr = slow.recv(4)
            (length,) = struct.unpack("!I", hdr)
            slow.recv(length)
            # Send only 2 bytes of a 4-byte length prefix and stall
            slow.sendall(b"\x00\x00")

            # Another client continues uninterrupted.
            with AayushSQLClient(*server.address, read_timeout=2.0) as healthy:
                assert healthy.ping()["pong"] is True

            assert _wait_for(lambda: server.session_count == 0, timeout=3.0)
        finally:
            slow.close()
    finally:
        server.shutdown()


def test_disconnect_during_large_response_leaves_server_healthy():
    server = AayushSQLServer(
        ServerConfig(db_path=None, port=0, write_timeout=2.0, read_timeout=5.0, shutdown_timeout=5.0)
    )
    server.start()
    try:
        with AayushSQLClient(*server.address) as setup:
            setup.execute("AAYUSH CREATE TABLE big (id INT, payload TEXT)")
            pad = "x" * 200
            values = ", ".join(f"({i}, '{pad}')" for i in range(1, 201))
            setup.execute(f"AAYUSH INSERT INTO big VALUES {values}")

        raw = socket.create_connection(server.address, timeout=2.0)
        raw.sendall(
            frame_bytes(
                encode_message(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "h-big",
                        "operation": "hello",
                        "client_name": "raw",
                        "client_version": "1.0.0",
                    }
                )
            )
        )
        raw.sendall(
            frame_bytes(
                encode_message(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "q-big",
                        "operation": "execute",
                        "sql": "AAYUSH SELECT * FROM big",
                    }
                )
            )
        )
        # Close abruptly without reading response
        raw.close()

        assert _wait_for(lambda: server.session_count == 0, timeout=3.0)
        with AayushSQLClient(*server.address) as verify:
            assert verify.execute("AAYUSH SELECT COUNT(*) AS n FROM big")["rows"] == [{"n": 200}]
    finally:
        assert server.shutdown() is True


# --- Phase 5.2: Multi-client stress across 1, 2, 4, 8, 16, 32 clients -----


@pytest.mark.parametrize("num_clients", [1, 2, 4, 8, 16, 32])
def test_multi_client_mixed_workload_stress(tmp_path: Path, num_clients: int):
    db_path = tmp_path / f"stress_{num_clients}.aaydb"
    server = AayushSQLServer(
        ServerConfig(
            db_path=db_path,
            port=0,
            max_clients=32,
            read_timeout=15.0,
            write_timeout=15.0,
            idle_timeout=30.0,
            shutdown_timeout=10.0,
        )
    )
    server.start()
    try:
        with AayushSQLClient(*server.address, read_timeout=10.0) as setup:
            setup.execute(
                "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, grp INT, balance INT, tag TEXT)"
            )
            setup.execute(
                "AAYUSH CREATE TABLE departments (grp INT PRIMARY KEY, title TEXT)"
            )
            setup.execute("AAYUSH CREATE INDEX idx_accounts_grp ON accounts (grp)")
            setup.execute(
                "AAYUSH INSERT INTO departments VALUES (0, 'Eng'), (1, 'Ops'), (2, 'Lab'), (3, 'Sec')"
            )
            # Pre-seed 150 rows for large-result, join, and aggregate queries.
            seed = ", ".join(
                f"({i}, {i % 4}, {100 + i}, 'seed_{i}')" for i in range(1, 151)
            )
            setup.execute(f"AAYUSH INSERT INTO accounts VALUES {seed}")

        errors: list[str] = []
        committed_workers: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(num_clients)

        def _exec_retry_busy(client: AayushSQLClient, sql: str) -> dict:
            deadline = time.monotonic() + 15.0
            while True:
                try:
                    return client.execute(sql)
                except ServerError as exc:
                    if exc.code == TRANSACTION_BUSY and time.monotonic() < deadline:
                        time.sleep(0.01)
                        continue
                    raise

        def _worker(worker_id: int) -> None:
            row_id = 1000 + worker_id
            grp = worker_id % 4
            try:
                with AayushSQLClient(*server.address, read_timeout=15.0) as client:
                    barrier.wait(timeout=10.0)

                    # 1. Explicit committed transaction (INSERT + UPDATE)
                    deadline = time.monotonic() + 15.0
                    while True:
                        try:
                            client.execute("AAYUSH BEGIN")
                            break
                        except ServerError as exc:
                            if exc.code == TRANSACTION_BUSY and time.monotonic() < deadline:
                                time.sleep(0.01)
                                continue
                            raise
                    try:
                        client.execute(
                            f"AAYUSH INSERT INTO accounts VALUES ({row_id}, {grp}, 500, 'w_{worker_id}')"
                        )
                        client.execute(
                            f"AAYUSH UPDATE accounts SET balance = balance + 25 WHERE id = {row_id}"
                        )
                        client.execute("AAYUSH COMMIT")
                    except Exception:
                        try:
                            client.execute("AAYUSH ROLLBACK")
                        except Exception:
                            pass
                        raise

                    with lock:
                        committed_workers.append(worker_id)

                    # 2. Explicit rolled-back transaction (must never become visible)
                    deadline = time.monotonic() + 15.0
                    while True:
                        try:
                            client.execute("AAYUSH BEGIN")
                            break
                        except ServerError as exc:
                            if exc.code == TRANSACTION_BUSY and time.monotonic() < deadline:
                                time.sleep(0.01)
                                continue
                            raise
                    client.execute(
                        f"AAYUSH INSERT INTO accounts VALUES ({5000 + worker_id}, 0, -999, 'ghost')"
                    )
                    client.execute("AAYUSH ROLLBACK")

                    # 3. Point SELECT verifying own committed row and no cross-session mix-up
                    own = _exec_retry_busy(
                        client,
                        f"AAYUSH SELECT id, balance, tag FROM accounts WHERE id = {row_id}",
                    )
                    assert own["rows"] == [
                        {"id": row_id, "balance": 525, "tag": f"w_{worker_id}"}
                    ], f"cross-session mismatch for worker {worker_id}: {own}"

                    # 4. Join + aggregate + EXPLAIN + large result set on subset of workers
                    if worker_id % 2 == 0:
                        joined = _exec_retry_busy(
                            client,
                            "AAYUSH SELECT d.title, COUNT(*) AS cnt, SUM(a.balance) AS total "
                            "FROM accounts AS a INNER JOIN departments AS d ON a.grp = d.grp "
                            "GROUP BY d.title ORDER BY d.title",
                        )
                        assert len(joined["rows"]) == 4
                    if worker_id == 0:
                        plan = _exec_retry_busy(
                            client,
                            "AAYUSH EXPLAIN SELECT id FROM accounts WHERE grp = 1",
                        )
                        assert "accounts" in plan["message"]
                        large = _exec_retry_busy(
                            client, "AAYUSH SELECT id, tag FROM accounts ORDER BY id"
                        )
                        assert large["row_count"] >= 150
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"worker {worker_id}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_clients)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)

        assert errors == []
        assert sorted(committed_workers) == list(range(num_clients))
        assert _wait_for(lambda: server.session_count == 0)

        with AayushSQLClient(*server.address) as verify:
            assert verify.execute("AAYUSH SELECT COUNT(*) AS n FROM accounts")["rows"] == [
                {"n": 150 + num_clients}
            ]
            assert (
                verify.execute("AAYUSH SELECT COUNT(*) AS g FROM accounts WHERE tag = 'ghost'")[
                    "rows"
                ]
                == [{"g": 0}]
            )
    finally:
        assert server.shutdown() is True

    # Restart server and verify durable recovery/persistence matches.
    restarted = AayushSQLServer(ServerConfig(db_path=db_path, port=0, max_clients=32))
    restarted.start()
    try:
        with AayushSQLClient(*restarted.address) as check:
            assert check.execute("AAYUSH SELECT COUNT(*) AS n FROM accounts")["rows"] == [
                {"n": 150 + num_clients}
            ]
            # Validate index integrity after stress
            restarted.engine.db.indexes["idx_accounts_grp"].validate()
    finally:
        assert restarted.shutdown() is True
