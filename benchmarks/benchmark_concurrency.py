"""Phase 4 concurrency benchmark: serial commits versus MVCC group commit.

The comparison uses the same number of durable autocommit transactions on the
same v4 database. ``serial`` executes one INSERT at a time on one session.
``group`` runs the same INSERTs from independent sessions, which may be admitted
while an earlier WAL fsync is still in flight and published by one shared fsync.

Run with::

    python benchmarks/benchmark_concurrency.py [--workers 8] [--per-worker 25]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql import Engine


def _run_serial(path: Path, count: int) -> dict:
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
    started = time.perf_counter()
    session = engine.connect()
    for row in range(count):
        session.execute(f"AAYUSH INSERT INTO g VALUES ({row})")
    elapsed = time.perf_counter() - started
    metrics = engine.mvcc.metrics()
    engine.close()
    return {"elapsed_s": elapsed, "txn_per_s": count / elapsed, **metrics}


def _run_group(path: Path, workers: int, per_worker: int) -> dict:
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
    barrier = threading.Barrier(workers)
    errors: list[str] = []

    def worker(worker_id: int) -> None:
        try:
            session = engine.connect()
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))
            return
        try:
            barrier.wait(timeout=30)
            for row in range(per_worker):
                session.execute(
                    f"AAYUSH INSERT INTO g VALUES ({worker_id * 1000 + row})"
                )
            session.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    elapsed = time.perf_counter() - started
    metrics = engine.mvcc.metrics()
    count = engine.execute("AAYUSH SELECT COUNT(*) AS n FROM g")["rows"][0]["n"]
    engine.close()
    return {
        "elapsed_s": elapsed,
        "txn_per_s": count / elapsed if elapsed else 0.0,
        "rows": count,
        "errors": errors,
        **metrics,
    }


def _reader_latency(path: Path, samples: int = 50) -> dict:
    engine = Engine(path)
    engine.execute("AAYUSH CREATE TABLE g (id INT PRIMARY KEY)")
    writer = engine.connect()
    reader = engine.connect()
    writer.execute("AAYUSH BEGIN")
    writer.execute("AAYUSH INSERT INTO g VALUES (999999)")
    latencies: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        reader.execute("AAYUSH SELECT COUNT(*) AS n FROM g")
        latencies.append((time.perf_counter() - started) * 1000.0)
    writer.execute("AAYUSH ROLLBACK")
    engine.close()
    return {
        "median_ms": statistics.median(latencies),
        "max_ms": max(latencies),
        "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--per-worker", type=int, default=25)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.per_worker < 1:
        parser.error("workers and per-worker must be positive")

    total = args.workers * args.per_worker
    with tempfile.TemporaryDirectory(prefix="aaysql-mvcc-bench-") as tmp:
        root = Path(tmp)
        serial = _run_serial(root / "serial.aaydb", total)
        group = _run_group(root / "group.aaydb", args.workers, args.per_worker)
        reader = _reader_latency(root / "reader.aaydb")

    result = {
        "workers": args.workers,
        "transactions": total,
        "serial": serial,
        "group": group,
        "reader_during_open_writer": reader,
        "speedup": max(group["txn_per_s"], serial["txn_per_s"])
        / min(group["txn_per_s"], serial["txn_per_s"]),
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print("Phase 4 concurrency benchmark")
    print(f"transactions: {total} ({args.workers} workers x {args.per_worker})")
    print(
        f"serial: {serial['txn_per_s']:.1f} txn/s, "
        f"{serial['fsyncs']} fsyncs, {serial['batches']} batches"
    )
    print(
        f"group:  {group['txn_per_s']:.1f} txn/s, "
        f"{group['fsyncs']} fsyncs, {group['batches']} batches, "
        f"rows={group['rows']}"
    )
    print(
        f"reader while writer is open: median "
        f"{reader['median_ms']:.2f} ms, max {reader['max_ms']:.2f} ms"
    )
    if group["errors"]:
        print(f"worker errors: {group['errors']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
