"""Reproducible benchmarks for the v0.8.0 client/server architecture.

Design notes
------------
* stdlib only (plus this package); no third-party benchmark dependency;
* a real server is started on ``127.0.0.1`` with an ephemeral port and every
  number is measured over real TCP sockets through :class:`AayushSQLClient`;
* the database is a temporary on-disk ``.aaydb`` file, so autocommit ``INSERT``
  numbers include the engine's normal WAL ``fsync`` durability cost;
* one warm-up iteration is discarded per scenario, then the median and the p95
  of the per-request wall clock are reported;
* transport cost is separated from database work where practical by comparing
  ``ping`` (framing + round trip, no SQL) with ``AAYUSH SELECT 1`` (minimum
  engine work) and with a larger result that also pays for row materialisation
  and transfer;
* execution is serialized by design, so the multi-client scenario reports the
  measured throughput and explicitly does **not** claim linear scaling.

Run with::

    python benchmarks/benchmark_server.py
"""

from __future__ import annotations

import json
import platform
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.client import AayushSQLClient
from aaysql.server import AayushSQLServer, ServerConfig

CONNECT_SAMPLES = 30
PING_SAMPLES = 300
SELECT_SAMPLES = 200
INSERT_SAMPLES = 100
CHURN_CYCLES = 50
MULTI_CLIENT_CLIENTS = 4
MULTI_CLIENT_REQUESTS = 60
LARGE_ROWS = 2_000
LARGE_TEXT = "x" * 200
SEED_ROWS = 1_000
WARMUPS = 1


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(round(fraction * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]


def _summarize(label: str, samples: list[float], unit: str = "ms") -> dict[str, float]:
    scale = 1_000.0 if unit == "ms" else 1.0
    return {
        "label": label,
        "n": len(samples),
        "median_ms": statistics.median(samples) * scale,
        "p95_ms": _percentile(samples, 0.95) * scale,
        "min_ms": min(samples) * scale,
        "max_ms": max(samples) * scale,
    }


def _print(row: dict[str, float], unit: str = "ms") -> None:
    print(
        f"  {row['label']:<34} n={row['n']:<5} "
        f"median={row['median_ms']:8.2f} {unit}  p95={row['p95_ms']:8.2f} {unit}  "
        f"min={row['min_ms']:7.2f}  max={row['max_ms']:8.2f}"
    )


def _timed(fn, samples: list[float]) -> None:
    started = time.perf_counter()
    fn()
    samples.append(time.perf_counter() - started)


def _bench_connect(server: AayushSQLServer) -> dict[str, float]:
    samples: list[float] = []
    for index in range(CONNECT_SAMPLES + WARMUPS):
        client = AayushSQLClient(*server.address)
        started = time.perf_counter()
        client.connect()
        elapsed = time.perf_counter() - started
        client.close()
        if index >= WARMUPS:
            samples.append(elapsed)
    return _summarize("connect + hello handshake", samples)


def _bench_ping(server: AayushSQLServer) -> dict[str, float]:
    samples: list[float] = []
    with AayushSQLClient(*server.address) as client:
        for index in range(PING_SAMPLES + WARMUPS):
            _timed(client.ping, samples if index >= WARMUPS else [])
            if index < WARMUPS:
                samples.clear()
    return _summarize("ping round trip (no SQL)", samples)


def _bench_select(server: AayushSQLServer) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with AayushSQLClient(*server.address) as client:
        client.execute(
            "AAYUSH CREATE TABLE bench_rows (id INT, name TEXT, payload TEXT)"
        )
        for index in range(SEED_ROWS):
            client.execute(
                f"AAYUSH INSERT INTO bench_rows VALUES ({index}, 'row{index}', '{LARGE_TEXT}')"
            )

        tiny: list[float] = []
        for index in range(SELECT_SAMPLES + WARMUPS):
            bucket = tiny if index >= WARMUPS else []
            _timed(
                lambda: client.execute("AAYUSH SELECT id FROM bench_rows WHERE id = 0"),
                bucket,
            )
        rows.append(_summarize("point SELECT, 1 column", tiny))

        one_row: list[float] = []
        for index in range(SELECT_SAMPLES + WARMUPS):
            bucket = one_row if index >= WARMUPS else []
            _timed(
                lambda: client.execute("AAYUSH SELECT * FROM bench_rows WHERE id = 500"),
                bucket,
            )
        rows.append(_summarize("point SELECT, 3 columns", one_row))

        full: list[float] = []
        for index in range(50 + WARMUPS):
            bucket = full if index >= WARMUPS else []
            _timed(lambda: client.execute("AAYUSH SELECT * FROM bench_rows"), bucket)
        rows.append(_summarize(f"full scan SELECT ({SEED_ROWS} rows)", full))

        client.execute("AAYUSH DROP TABLE bench_rows")
    return rows


def _bench_insert(server: AayushSQLServer) -> dict[str, float]:
    samples: list[float] = []
    with AayushSQLClient(*server.address) as client:
        client.execute("AAYUSH CREATE TABLE insert_bench (id INT, name TEXT)")
        try:
            for index in range(INSERT_SAMPLES + WARMUPS):
                bucket = samples if index >= WARMUPS else []
                _timed(
                    lambda index=index: client.execute(
                        f"AAYUSH INSERT INTO insert_bench VALUES ({index}, 'v{index}')"
                    ),
                    bucket,
                )
        finally:
            client.execute("AAYUSH DROP TABLE insert_bench")
    return _summarize("INSERT autocommit (WAL fsync)", samples)


def _bench_transaction(server: AayushSQLServer) -> dict[str, float]:
    samples: list[float] = []
    with AayushSQLClient(*server.address) as client:
        client.execute("AAYUSH CREATE TABLE tx_bench (id INT, name TEXT)")
        try:
            for index in range(10 + WARMUPS):
                bucket = samples if index >= WARMUPS else []

                def work(index: int = index) -> None:
                    client.execute("AAYUSH BEGIN")
                    for row in range(20):
                        client.execute(
                            f"AAYUSH INSERT INTO tx_bench VALUES ({index * 100 + row}, 'r{row}')"
                        )
                    client.execute("AAYUSH COMMIT")

                _timed(work, bucket)
        finally:
            client.execute("AAYUSH DROP TABLE tx_bench")
    return _summarize("BEGIN + 20 INSERT + COMMIT", samples)


def _bench_multi_client(server: AayushSQLServer) -> dict[str, float]:
    clients = [AayushSQLClient(*server.address) for _ in range(MULTI_CLIENT_CLIENTS)]
    for client in clients:
        client.connect()
    errors: list[BaseException] = []
    latencies: list[float] = []

    def worker(index: int) -> None:
        client = clients[index]
        try:
            for _ in range(MULTI_CLIENT_REQUESTS):
                started = time.perf_counter()
                client.ping()
                latencies.append(time.perf_counter() - started)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(clients))]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    for client in clients:
        client.close()

    total = len(latencies)
    summary = _summarize(
        f"{len(clients)} clients x {MULTI_CLIENT_REQUESTS} pings", latencies
    )
    summary["throughput"] = total / elapsed if elapsed else 0.0
    summary["failures"] = float(len(errors))
    return summary


def _bench_churn(server: AayushSQLServer) -> dict[str, float]:
    samples: list[float] = []
    for index in range(CHURN_CYCLES + WARMUPS):
        bucket = samples if index >= WARMUPS else []

        def cycle() -> None:
            client = AayushSQLClient(*server.address)
            client.connect()
            client.ping()
            client.close()

        _timed(cycle, bucket)
    return _summarize("connect + ping + close", samples)


def _bench_large_result(
    server: AayushSQLServer, db_path: Path
) -> dict[str, float]:
    with AayushSQLClient(*server.address) as client:
        client.execute("AAYUSH CREATE TABLE big_rows (id INT, payload TEXT)")
        try:
            for index in range(LARGE_ROWS):
                client.execute(
                    f"AAYUSH INSERT INTO big_rows VALUES ({index}, '{LARGE_TEXT}')"
                )
            response = client.execute("AAYUSH SELECT * FROM big_rows")
            row_count = response["row_count"]
            assert row_count == LARGE_ROWS, row_count
            # Size of the returned row payload as JSON, i.e. before framing.
            wire_bytes = len(json.dumps(response["rows"]).encode("utf-8"))
            db_bytes = db_path.stat().st_size if db_path.exists() else 0
            samples: list[float] = []
            for index in range(20 + WARMUPS):
                bucket = samples if index >= WARMUPS else []
                _timed(lambda: client.execute("AAYUSH SELECT * FROM big_rows"), bucket)
        finally:
            client.execute("AAYUSH DROP TABLE big_rows")
    summary = _summarize(
        f"SELECT {LARGE_ROWS} rows x {len(LARGE_TEXT)} chars", samples
    )
    summary["payload_bytes"] = float(wire_bytes)
    summary["db_bytes"] = float(db_bytes)
    return summary


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="aaysql-bench-"))
    db_path = workdir / "bench.aaydb"
    config = ServerConfig(db_path=db_path, port=0)
    server = AayushSQLServer(config)
    server.start()
    print("AayushSQL v0.8.0 client/server benchmarks")
    print(f"  server: {server.address[0]}:{server.address[1]} (protocol 1)")
    print(f"  database: {db_path}")
    print(f"  os: {platform.platform()}")
    print(f"  python: {platform.python_version()} ({platform.python_implementation()})")
    print(f"  clients in multi-client scenario: {MULTI_CLIENT_CLIENTS}")
    print(
        f"  total requests: {CONNECT_SAMPLES + PING_SAMPLES + SELECT_SAMPLES * 3 + INSERT_SAMPLES + 20 + MULTI_CLIENT_CLIENTS * MULTI_CLIENT_REQUESTS + CHURN_CYCLES + 20}"
    )
    print()

    try:
        print("Connection and transport")
        _print(_bench_connect(server))
        _print(_bench_ping(server))
        _print(_bench_churn(server))

        print("\nSQL execution")
        for row in _bench_select(server):
            _print(row)
        _print(_bench_insert(server))
        _print(_bench_transaction(server))

        print("\nLarge result transfer")
        large = _bench_large_result(server, db_path)
        _print(large)
        print(
            f"  payload {large['payload_bytes'] / 1_000_000:.2f} MB "
            f"(frame limit {config.max_frame_size / (1024 * 1024):.0f} MiB)"
        )
        print(f"  database size while loaded: {large['db_bytes'] / 1_000_000:.2f} MB")

        print("\nMultiple simultaneous clients (serialized execution)")
        multi = _bench_multi_client(server)
        _print(multi)
        print(
            f"  throughput: {multi['throughput']:.1f} requests/s across "
            f"{MULTI_CLIENT_CLIENTS} clients; failures={int(multi['failures'])}"
        )
    finally:
        server.shutdown()
        db_bytes = db_path.stat().st_size if db_path.exists() else 0
        print(
            f"\nDatabase size after cleanup (benchmark tables dropped): "
            f"{db_bytes / 1_000_000:.2f} MB"
        )
        print(f"Server stats: {server.stats}")
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
