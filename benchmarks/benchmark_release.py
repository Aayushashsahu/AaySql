"""Reproducible v1.0.0 release benchmark suite (Phase 6).

Measures storage, query engine, client/server, and repeated-operation memory
growth under deterministic datasets. Outputs a Markdown summary suitable for
RELEASE_BENCHMARKS.md.

Run with::

    python benchmarks/benchmark_release.py
"""

from __future__ import annotations

import json
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql import Engine, __version__
from aaysql.backup import backup_database, restore_database
from aaysql.client import AayushSQLClient
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import plan_select
from aaysql.server import AayushSQLServer, ServerConfig
from aaysql.storage import ENGINE_VERSION, FORMAT_VERSION
from aaysql.wal import WalManager

SEED = 20261111


def _seed_v3_database(path: Path) -> None:
    """Create an empty format-v3 database so this suite measures the v3 baseline.

    New databases are format v4; the release benchmark documents the legacy
    whole-image storage path, which is still fully supported.
    """
    if path.exists():
        return
    path.write_text(
        json.dumps(
            {
                "format": "aaysql",
                "format_version": FORMAT_VERSION,
                "engine_version": ENGINE_VERSION,
                "wal_lsn": 0,
                "tables": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )


def _parse(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _bench(label: str, fn, *, repeats: int = 7, warmups: int = 1) -> dict:
    for _ in range(warmups):
        fn()
    samples_ms: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
    return {
        "label": label,
        "n": repeats,
        "median_ms": statistics.median(samples_ms),
        "min_ms": min(samples_ms),
        "max_ms": max(samples_ms),
    }


def _bulk_insert(engine: Engine, table: str, rows: list[str], chunk: int = 500) -> None:
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        if batch:
            engine.execute(f"AAYUSH INSERT INTO {table} VALUES " + ", ".join(batch))


def run_storage_benchmarks() -> list[dict]:
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="aaysql-relbench-storage-") as tmp:
        root = Path(tmp)

        # Seed a 1,000-row database with an index for open/close/backup/restore.
        seed_db = root / "seed.aaydb"
        _seed_v3_database(seed_db)
        e = Engine(seed_db)
        e.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, grp INT, title TEXT)")
        e.execute("AAYUSH CREATE INDEX idx_items_grp ON items (grp)")
        _bulk_insert(
            e,
            "items",
            [f"({i}, {i % 25}, 'item_{i}')" for i in range(1, 1001)],
        )
        e.close()

        results.append(
            _bench(
                "Database open (1,000 rows, index rebuild)",
                lambda: Engine(seed_db).close(),
                repeats=7,
            )
        )

        # Close benchmark on a cleanly opened engine
        def _open_close():
            eng = Engine(seed_db)
            t0 = time.perf_counter()
            eng.close()
            return (time.perf_counter() - t0) * 1000.0

        # Measure close directly
        close_samples: list[float] = []
        for idx in range(8):
            ms = _open_close()
            if idx >= 1:
                close_samples.append(ms)
        results.append(
            {
                "label": "Database close (clean)",
                "n": len(close_samples),
                "median_ms": statistics.median(close_samples),
                "min_ms": min(close_samples),
                "max_ms": max(close_samples),
            }
        )

        # WAL autocommit INSERT & checkpoint
        auto_db = root / "autocommit.aaydb"
        _seed_v3_database(auto_db)
        auto_engine = Engine(auto_db)
        auto_engine.execute("AAYUSH CREATE TABLE log_rows (id INT PRIMARY KEY, v INT)")
        counter = {"id": 1}

        def _autocommit_insert():
            i = counter["id"]
            counter["id"] += 1
            auto_engine.execute(f"AAYUSH INSERT INTO log_rows VALUES ({i}, {i * 2})")

        results.append(_bench("WAL autocommit INSERT (small table)", _autocommit_insert, repeats=15))
        auto_engine.close()

        # Explicit transaction with 50 mutations (single COMMIT)
        tx_db = root / "tx.aaydb"
        _seed_v3_database(tx_db)
        tx_engine = Engine(tx_db)
        tx_engine.execute("AAYUSH CREATE TABLE tx_rows (id INT PRIMARY KEY, v INT)")
        tx_id = {"n": 0}

        def _explicit_tx_50():
            base = tx_id["n"] * 50
            tx_id["n"] += 1
            tx_engine.execute("AAYUSH BEGIN")
            for k in range(1, 51):
                tx_engine.execute(f"AAYUSH INSERT INTO tx_rows VALUES ({base + k}, {k})")
            tx_engine.execute("AAYUSH COMMIT")

        results.append(_bench("Explicit BEGIN + 50 INSERTs + COMMIT", _explicit_tx_50, repeats=7))

        # Direct checkpoint timing on the tx_db state
        results.append(
            _bench(
                "Checkpoint atomic write + fsync (~350 rows)",
                lambda: tx_engine.storage._checkpoint(tx_engine.db, tx_engine.db.wal_lsn),
                repeats=7,
            )
        )
        tx_engine.close()

        # WAL recovery time on reopen (pending WAL with 1,000 rows)
        rec_db = root / "recover.aaydb"
        shutil.copy2(seed_db, rec_db)
        base_payload = json.loads(rec_db.read_text(encoding="utf-8"))
        wal_payload = dict(base_payload)
        wal_payload["tables"] = {
            "items": {
                **base_payload["tables"]["items"],
                "rows": base_payload["tables"]["items"]["rows"]
                + [{"id": 1001, "grp": 1, "title": "pending_wal_row"}],
            }
        }
        rec_samples: list[float] = []
        for idx in range(8):
            shutil.copy2(seed_db, rec_db)
            wal_mgr = WalManager(rec_db)
            wal_mgr.last_lsn = base_payload["wal_lsn"]
            wal_mgr.append_commit_transaction(wal_payload)
            t0 = time.perf_counter()
            eng = Engine(rec_db)
            elapsed = (time.perf_counter() - t0) * 1000.0
            eng.close()
            if idx >= 1:
                rec_samples.append(elapsed)
        results.append(
            {
                "label": "WAL recovery on open (1,001 rows + checkpoint)",
                "n": len(rec_samples),
                "median_ms": statistics.median(rec_samples),
                "min_ms": min(rec_samples),
                "max_ms": max(rec_samples),
            }
        )

        # Backup & restore timing (1,000 rows + index)
        bkp_dir = root / "backup_out"
        restored_db = root / "restored.aaydb"
        results.append(
            _bench(
                "Offline backup (1,000 rows + index)",
                lambda: backup_database(seed_db, bkp_dir, force=True),
                repeats=5,
            )
        )
        results.append(
            _bench(
                "Offline restore (verify + validate + install)",
                lambda: restore_database(bkp_dir, restored_db, force=True),
                repeats=5,
            )
        )
    return results


def run_query_benchmarks() -> list[dict]:
    rng = random.Random(SEED)
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE docs (id INT, dept TEXT, score INT, title TEXT)")
    depts = ("Eng", "Sales", "Ops", "Lab", "Sec", "HR", "Fin")
    _bulk_insert(
        engine,
        "docs",
        [
            f"({i}, '{rng.choice(depts)}', {rng.randint(1, 100)}, 'doc_{i % 50}')"
            for i in range(1, 5001)
        ],
    )
    engine.execute("AAYUSH CREATE INDEX idx_docs_score ON docs (score)")

    engine.execute("AAYUSH CREATE TABLE users (id INT, tier INT)")
    engine.execute("AAYUSH CREATE TABLE orders (id INT, user_id INT, amount INT)")
    _bulk_insert(engine, "users", [f"({i}, {i % 5})" for i in range(1, 1501)])
    _bulk_insert(
        engine,
        "orders",
        [f"({i}, {rng.randint(1, 1500)}, {rng.randint(10, 500)})" for i in range(1, 2001)],
    )

    hash_stmt = _parse(
        "AAYUSH SELECT u.id, o.amount FROM users AS u INNER JOIN orders AS o ON u.id = o.user_id"
    )
    nl_stmt = _parse(
        "AAYUSH SELECT u.id, o.amount FROM users AS u INNER JOIN orders AS o ON u.id = o.user_id"
    )
    hash_plan = plan_select(engine.db, hash_stmt, force_join_algorithm="HASH")
    nl_plan = plan_select(engine.db, nl_stmt, force_join_algorithm="NESTED LOOP")

    # Small join subset for nested-loop baseline (300 x 400) so benchmark stays fast
    engine.execute("AAYUSH CREATE TABLE u_small (id INT)")
    engine.execute("AAYUSH CREATE TABLE o_small (id INT, user_id INT)")
    _bulk_insert(engine, "u_small", [f"({i})" for i in range(1, 301)])
    _bulk_insert(engine, "o_small", [f"({i}, {(i % 300) + 1})" for i in range(1, 401)])
    nl_small_stmt = _parse(
        "AAYUSH SELECT u.id, o.id FROM u_small AS u INNER JOIN o_small AS o ON u.id = o.user_id"
    )
    nl_small_plan = plan_select(engine.db, nl_small_stmt, force_join_algorithm="NESTED LOOP")
    hash_small_plan = plan_select(engine.db, nl_small_stmt, force_join_algorithm="HASH")
    _ = (nl_plan,)

    scan_stmt = _parse("AAYUSH SELECT id FROM docs WHERE score = 42")
    forced_scan_plan = plan_select(engine.db, scan_stmt, force_access_path="TABLE")

    results = [
        _bench(
            "Indexed equality lookup (5,000 rows)",
            lambda: engine.execute("AAYUSH SELECT id FROM docs WHERE score = 42"),
        ),
        _bench(
            "Indexed range scan (BETWEEN 20 AND 30, 5,000 rows)",
            lambda: engine.execute("AAYUSH SELECT id FROM docs WHERE score BETWEEN 20 AND 30"),
        ),
        _bench(
            "Forced full table scan (5,000 rows)",
            lambda: execute_select(engine.db, scan_stmt, plan=forced_scan_plan),
        ),
        _bench(
            "Hash join (1,500 x 2,000 equi-join)",
            lambda: execute_select(engine.db, hash_stmt, plan=hash_plan),
        ),
        _bench(
            "Hash join (300 x 400 baseline pair)",
            lambda: execute_select(engine.db, nl_small_stmt, plan=hash_small_plan),
        ),
        _bench(
            "Nested-loop join (300 x 400 baseline pair)",
            lambda: execute_select(engine.db, nl_small_stmt, plan=nl_small_plan),
        ),
        _bench(
            "GROUP BY dept + COUNT/AVG (5,000 rows)",
            lambda: engine.execute(
                "AAYUSH SELECT dept, COUNT(*) AS c, AVG(score) AS a FROM docs GROUP BY dept"
            ),
        ),
        _bench(
            "SELECT DISTINCT title (5,000 rows -> 50)",
            lambda: engine.execute("AAYUSH SELECT DISTINCT title FROM docs"),
        ),
        _bench(
            "LIKE '%doc_1%' filter (5,000 rows)",
            lambda: engine.execute("AAYUSH SELECT id FROM docs WHERE title LIKE '%doc_1%'"),
        ),
        _bench(
            "CASE 3-way projection (5,000 rows)",
            lambda: engine.execute(
                "AAYUSH SELECT id, CASE WHEN score >= 80 THEN 'A' WHEN score >= 50 THEN 'B' ELSE 'C' END AS grade FROM docs"
            ),
        ),
        _bench(
            "ORDER BY + OFFSET 500 LIMIT 50 (5,000 rows)",
            lambda: engine.execute(
                "AAYUSH SELECT id, score FROM docs ORDER BY score DESC, id LIMIT 50 OFFSET 500"
            ),
        ),
        _bench(
            "EXPLAIN (non-executing plan)",
            lambda: engine.execute(
                "AAYUSH EXPLAIN SELECT u.id, o.amount FROM users AS u INNER JOIN orders AS o ON u.id = o.user_id"
            ),
        ),
        _bench(
            "EXPLAIN ANALYZE (1,500 x 2,000 hash join)",
            lambda: engine.execute(
                "AAYUSH EXPLAIN ANALYZE SELECT u.id, o.amount FROM users AS u INNER JOIN orders AS o ON u.id = o.user_id"
            ),
        ),
    ]
    engine.close()
    return results


def run_server_benchmarks() -> list[dict]:
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="aaysql-relbench-srv-") as tmp:
        db_path = Path(tmp) / "srv.aaydb"
        server = AayushSQLServer(ServerConfig(db_path=db_path, port=0, max_clients=16))
        server.start()
        try:
            with AayushSQLClient(*server.address) as setup:
                setup.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
                pad = "x" * 100
                _bulk_insert_client(
                    setup,
                    [f"({i}, '{pad}')" for i in range(1, 1001)],
                )

            def _handshake():
                with AayushSQLClient(*server.address) as c:
                    _ = c.server_version

            results.append(_bench("TCP connect + hello handshake", _handshake, repeats=15))

            with AayushSQLClient(*server.address) as client:
                results.append(_bench("TCP ping", client.ping, repeats=25))
                results.append(
                    _bench(
                        "TCP point SELECT (1 row)",
                        lambda: client.execute("AAYUSH SELECT id FROM t WHERE id = 500"),
                        repeats=25,
                    )
                )
                results.append(
                    _bench(
                        "TCP large result SELECT (1,000 rows)",
                        lambda: client.execute("AAYUSH SELECT * FROM t ORDER BY id"),
                        repeats=10,
                    )
                )
                next_id = {"n": 2000}

                def _auto_ins():
                    nid = next_id["n"]
                    next_id["n"] += 1
                    client.execute(f"AAYUSH INSERT INTO t VALUES ({nid}, 'new')")

                results.append(_bench("TCP autocommit INSERT (WAL fsync)", _auto_ins, repeats=15))

                def _tx():
                    nid = next_id["n"]
                    next_id["n"] += 1
                    client.execute("AAYUSH BEGIN")
                    client.execute(f"AAYUSH INSERT INTO t VALUES ({nid}, 'tx')")
                    client.execute("AAYUSH COMMIT")

                results.append(_bench("TCP explicit BEGIN + INSERT + COMMIT", _tx, repeats=10))

            # Multiple connected clients (4 concurrent point SELECTs)
            def _multi_4():
                errs: list[BaseException] = []

                def _w():
                    try:
                        with AayushSQLClient(*server.address) as c:
                            for _ in range(5):
                                c.execute("AAYUSH SELECT id FROM t WHERE id = 10")
                    except BaseException as exc:
                        errs.append(exc)

                threads = [threading.Thread(target=_w) for _ in range(4)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=15)
                assert not errs

            results.append(_bench("TCP 4 concurrent clients x 5 SELECTs", _multi_4, repeats=5))
        finally:
            server.shutdown()
    return results


def _bulk_insert_client(client: AayushSQLClient, rows: list[str], chunk: int = 250) -> None:
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        if batch:
            client.execute("AAYUSH INSERT INTO t VALUES " + ", ".join(batch))


def run_memory_growth_check() -> dict[str, float]:
    tracemalloc.start()
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE mem (id INT PRIMARY KEY, v INT)")
    _bulk_insert(engine, "mem", [f"({i}, {i % 10})" for i in range(1, 201)])
    # Warm up statistics & pattern caches
    for _ in range(10):
        engine.execute("AAYUSH SELECT id FROM mem WHERE v = 3 AND 'abc' LIKE 'a%'")
    snap_base, _ = tracemalloc.get_traced_memory()
    for i in range(500):
        engine.execute(f"AAYUSH SELECT id FROM mem WHERE v = {i % 10} AND 'abc' LIKE 'a%'")
    snap_after, peak = tracemalloc.get_traced_memory()
    engine.close()

    server = AayushSQLServer(ServerConfig(db_path=None, port=0))
    server.start()
    try:
        with AayushSQLClient(*server.address) as c:
            c.ping()
        srv_base, _ = tracemalloc.get_traced_memory()
        for _ in range(40):
            with AayushSQLClient(*server.address) as c:
                c.ping()
        srv_after, srv_peak = tracemalloc.get_traced_memory()
    finally:
        server.shutdown()

    tracemalloc.stop()
    return {
        "engine_500_queries_delta_kib": (snap_after - snap_base) / 1024.0,
        "engine_peak_kib": peak / 1024.0,
        "server_40_sessions_delta_kib": (srv_after - srv_base) / 1024.0,
        "server_peak_kib": srv_peak / 1024.0,
    }


def main() -> None:
    print(f"AayushSQL {__version__} (legacy format v{FORMAT_VERSION} baseline)")
    print(
        f"OS: {platform.platform()} | Python: {platform.python_version()} | "
        f"CPU: {platform.processor() or platform.machine()} ({os.cpu_count()} cores)"
    )
    print("\n--- Storage & WAL / Backup ---")
    storage_res = run_storage_benchmarks()
    for row in storage_res:
        print(
            f"| {row['label']} | {row['n']} | {row['median_ms']:.2f} ms | "
            f"{row['min_ms']:.2f}-{row['max_ms']:.2f} ms |"
        )

    print("\n--- Query Engine (In-Memory) ---")
    query_res = run_query_benchmarks()
    for row in query_res:
        print(
            f"| {row['label']} | {row['n']} | {row['median_ms']:.2f} ms | "
            f"{row['min_ms']:.2f}-{row['max_ms']:.2f} ms |"
        )

    print("\n--- Client / Server (Loopback TCP, On-Disk DB) ---")
    srv_res = run_server_benchmarks()
    for row in srv_res:
        print(
            f"| {row['label']} | {row['n']} | {row['median_ms']:.2f} ms | "
            f"{row['min_ms']:.2f}-{row['max_ms']:.2f} ms |"
        )

    print("\n--- Memory Growth Check (tracemalloc) ---")
    mem = run_memory_growth_check()
    print(mem)


if __name__ == "__main__":
    main()
