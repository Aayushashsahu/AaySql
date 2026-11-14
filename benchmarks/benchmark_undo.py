"""Transaction-management benchmark: in-memory rollback vs. durable commit overhead.

This benchmark is deliberately split from the storage benchmarks: it measures the
cost of ``BEGIN``, statement markers, rollback, and commit bookkeeping, not SQL
scan cost. ``UPDATE`` / ``DELETE ... WHERE id = ?`` still scan the table because
there is no index on ``id`` (only on ``value``), so their numbers include that
scan. The database is seeded directly once per size and every rollback case
restores the same rows, so later repetitions start from an identical table.

Run:

    PYTHONPATH=src python benchmarks/benchmark_undo.py
    PYTHONPATH=src python benchmarks/benchmark_undo.py --json

The same script runs against the Phase 2 (snapshot-per-statement) engine and the
Phase 3 (undo journal) engine; the table shape is identical so the two runs can
be compared directly.
"""

from __future__ import annotations

import argparse
import itertools
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aaysql import Engine
from aaysql.ast import ColumnDef
from aaysql.errors import ExecutionError
from aaysql.storage import Database, Table

SIZES = (100, 1_000, 10_000, 100_000)
REPETITIONS = 3
WARMUP = 1
# A snapshot-per-statement baseline costs ~0.9 s per clone at 100k rows on the
# reference machine, so the case size stays small enough for the Phase 2 and
# Phase 3 runs to use the same script and settings.
MUTATIONS = 10
MEMORY_UPDATES = 10
DDL_ROUNDS = 5
COLUMNS = [
    ColumnDef("id", "INT", primary_key=True),
    ColumnDef("name", "TEXT"),
    ColumnDef("value", "INT"),
    ColumnDef("note", "TEXT"),
]


def _seed_engine(rows: int) -> Engine:
    """Build an in-memory engine with a populated table and one secondary index."""
    engine = Engine()
    db = Database()
    table = Table("bench", list(COLUMNS))
    table.rows = [
        {"id": i, "name": f"row-{i}", "value": i % 1000, "note": "x" * 12}
        for i in range(1, rows + 1)
    ]
    db.tables["bench"] = table
    db.create_index("idx_bench_value", "bench", "value")
    engine.db = db
    engine.executor.db = db
    return engine


def _timed(action: Callable[[], Any], repetitions: int, warmup: int) -> dict[str, float]:
    for _ in range(warmup):
        action()
    samples: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        action()
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[p95_index],
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def _insert_sql(engine: Engine, count: int, ids: Any) -> None:
    engine.execute(
        "AAYUSH INSERT INTO bench VALUES "
        + ", ".join(f"({next(ids)}, 'n', 1, 'x')" for _ in range(count))
    )


def _rollback_inserts(engine: Engine) -> None:
    engine.execute("AAYUSH BEGIN")
    for offset in range(MUTATIONS):
        engine.execute(
            f"AAYUSH INSERT INTO bench VALUES ({800000 + offset}, 'n', 1, 'x')"
        )
    engine.execute("AAYUSH ROLLBACK")


def _rollback_updates(engine: Engine) -> None:
    engine.execute("AAYUSH BEGIN")
    for value in range(1, MUTATIONS + 1):
        engine.execute(
            f"AAYUSH UPDATE bench SET value = value + 1 WHERE id = {value}"
        )
    engine.execute("AAYUSH ROLLBACK")


def _rollback_deletes(engine: Engine) -> None:
    engine.execute("AAYUSH BEGIN")
    for value in range(1, MUTATIONS + 1):
        engine.execute(f"AAYUSH DELETE FROM bench WHERE id = {value}")
    engine.execute("AAYUSH ROLLBACK")


def _rollback_ddl(engine: Engine) -> None:
    engine.execute("AAYUSH BEGIN")
    for round_number in range(DDL_ROUNDS):
        name = f"idx_ddl_{round_number}"
        engine.execute(f"AAYUSH CREATE INDEX {name} ON bench (value)")
        engine.execute(f"AAYUSH DROP INDEX {name}")
    engine.execute("AAYUSH ROLLBACK")


def _failed_statement(engine: Engine) -> None:
    engine.execute("AAYUSH BEGIN")
    try:
        # The final row duplicates an existing primary key; the whole batch fails.
        engine.execute(
            "AAYUSH INSERT INTO bench VALUES (700001, 'n', 1, 'x'), "
            "(1, 'duplicate', 1, 'x')"
        )
    except ExecutionError:
        pass
    engine.execute("AAYUSH ROLLBACK")


def _memory_transaction(engine: Engine) -> tuple[float, float]:
    tracemalloc.start()
    try:
        engine.execute("AAYUSH BEGIN")
        for value in range(1, MEMORY_UPDATES + 1):
            engine.execute(
                f"AAYUSH UPDATE bench SET value = value + 1 WHERE id = {value}"
            )
        peak, _current = tracemalloc.get_traced_memory()
        engine.execute("AAYUSH ROLLBACK")
    finally:
        tracemalloc.stop()
    return peak / 1024.0, 0.0


def _cases(engine: Engine) -> dict[str, Callable[[], Any]]:
    committed_ids = itertools.count(900000)
    statement_ids = itertools.count(810000)
    autocommit_ids = itertools.count(850000, 10)

    def begin_rollback() -> None:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH ROLLBACK")

    def txn_rows(count: int) -> None:
        engine.execute("AAYUSH BEGIN")
        _insert_sql(engine, count, committed_ids)
        engine.execute("AAYUSH COMMIT")

    def multi_statement() -> None:
        engine.execute("AAYUSH BEGIN")
        for _ in range(MUTATIONS):
            engine.execute(
                f"AAYUSH INSERT INTO bench VALUES ({next(statement_ids)}, 'n', 1, 'x')"
            )
        engine.execute("AAYUSH COMMIT")

    def autocommit() -> None:
        engine.execute(
            f"AAYUSH INSERT INTO bench VALUES ({next(autocommit_ids)}, 'n', 1, 'x')"
        )

    return {
        "begin_rollback": begin_rollback,
        "txn_1_row": lambda: txn_rows(1),
        "txn_10_rows": lambda: txn_rows(10),
        "txn_100_rows": lambda: txn_rows(100),
        "multi_statement_10": multi_statement,
        "rollback_inserts": lambda: _rollback_inserts(engine),
        "rollback_updates": lambda: _rollback_updates(engine),
        "rollback_deletes": lambda: _rollback_deletes(engine),
        "rollback_ddl": lambda: _rollback_ddl(engine),
        "failed_statement": lambda: _failed_statement(engine),
        "autocommit_insert": autocommit,
        "commit_no_mutations": lambda: (
            engine.execute("AAYUSH BEGIN"),
            engine.execute("AAYUSH COMMIT"),
        ),
    }


def _persistent_autocommit(rows: int) -> dict[str, float] | None:
    """Autocommit on a real v4 file, seeded directly through the storage layer."""
    from aaysql.storage import SegmentedStorage

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "undo_bench.aaydb"
        storage = SegmentedStorage(path)
        storage.load()
        db = Database()
        table = Table("bench", list(COLUMNS))
        table.rows = [
            {"id": i, "name": f"row-{i}", "value": i % 1000, "note": "x" * 12}
            for i in range(1, rows + 1)
        ]
        db.tables["bench"] = table
        db.create_index("idx_bench_value", "bench", "value")
        storage.checkpoint(db)
        engine = Engine(path)
        try:
            counter = itertools.count(950000)

            def action() -> None:
                engine.execute(
                    f"AAYUSH INSERT INTO bench VALUES ({next(counter)}, 'n', 1, 'x')"
                )

            return _timed(action, REPETITIONS, WARMUP)
        except Exception as exc:  # noqa: BLE001 - benchmark diagnostic
            print(f"persistent autocommit failed at {rows} rows: {exc}")
            return None
        finally:
            engine.close()


def run() -> dict[str, Any]:
    results: dict[str, Any] = {
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "cpu": platform.processor(),
            "sizes": list(SIZES),
            "repetitions": REPETITIONS,
            "warmup": WARMUP,
            "mutations_per_case": MUTATIONS,
            "memory_probe_updates": MEMORY_UPDATES,
            "storage": "in-memory engine; one secondary index on value; "
            "no index on id, so UPDATE/DELETE include a table scan",
        },
        "sizes": {},
        "persistent_autocommit": {},
    }
    for rows in SIZES:
        print(f"[benchmark] {rows:,} rows: seeding", file=sys.stderr, flush=True)
        engine = _seed_engine(rows)
        try:
            size_results: dict[str, Any] = {}
            for name, action in _cases(engine).items():
                print(
                    f"[benchmark] {rows:,} rows: {name}", file=sys.stderr, flush=True
                )
                size_results[name] = _timed(action, REPETITIONS, WARMUP)
            print(
                f"[benchmark] {rows:,} rows: transaction_memory",
                file=sys.stderr,
                flush=True,
            )
            peak_kib, _ = _memory_transaction(engine)
            size_results["transaction_memory"] = {"peak_kib": peak_kib}
            results["sizes"][str(rows)] = size_results
        finally:
            engine.close()
        print(
            f"[benchmark] {rows:,} rows: persistent autocommit",
            file=sys.stderr,
            flush=True,
        )
        peak = _persistent_autocommit(rows)
        if peak is not None:
            results["persistent_autocommit"][str(rows)] = peak
    return results


def _print_table(results: dict[str, Any]) -> None:
    environment = results["environment"]
    print("AayushSQL transaction benchmark")
    print(
        f"OS: {environment['os']} | Python: {environment['python']} | "
        f"CPU: {environment['cpu'] or 'unknown'}"
    )
    print(
        f"Repetitions: {environment['repetitions']} (median / p95), "
        f"warm-up {environment['warmup']}, {environment['mutations_per_case']} mutations per case"
    )
    print(f"Storage: {environment['storage']}\n")

    case_names = list(results["sizes"][str(SIZES[0])].keys())
    print("| Case | " + " | ".join(f"{rows:,} rows" for rows in SIZES) + " |")
    print("| --- | " + " | ".join("---:" for _ in SIZES) + " |")
    for case in case_names:
        cells = []
        for rows in SIZES:
            entry = results["sizes"][str(rows)][case]
            if case == "transaction_memory":
                cells.append(f"{entry['peak_kib']:.0f} KiB peak")
            else:
                cells.append(f"{entry['median_ms']:.2f} / {entry['p95_ms']:.2f} ms")
        print(f"| {case} | " + " | ".join(cells) + " |")

    if results["persistent_autocommit"]:
        print("\nPersistent v4 autocommit INSERT (median / p95):")
        for rows, entry in results["persistent_autocommit"].items():
            print(f"- {int(rows):,} rows: {entry['median_ms']:.2f} / {entry['p95_ms']:.2f} ms")


def main() -> None:
    global SIZES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print raw JSON results")
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in SIZES),
        help="comma-separated row counts (default: all four sizes)",
    )
    args = parser.parse_args()
    SIZES = tuple(int(value) for value in args.sizes.split(",") if value.strip())
    results = run()
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        _print_table(results)


if __name__ == "__main__":
    main()
