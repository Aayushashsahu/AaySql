"""Reproducible AayushSQL index benchmarks.

Measures table scan versus B+ tree index access, index build cost, mutation
overhead, and storage overhead. Measurements only: there are no timing assertions
and this script is not part of CI.

Usage:
    python benchmarks/benchmark_index.py [--sizes 1000,10000,100000]
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aaysql import Engine, __version__  # noqa: E402

CHUNK = 2000


def build_engine(rows: int, *, with_index: bool) -> Engine:
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE bench (id INT, age INT, name TEXT)")
    for start in range(0, rows, CHUNK):
        stop = min(start + CHUNK, rows)
        values = ", ".join(f"({i}, {i}, 'user{i}')" for i in range(start, stop))
        engine.execute(f"AAYUSH INSERT INTO bench VALUES {values}")
    if with_index:
        engine.execute("AAYUSH CREATE INDEX idx_bench_age ON bench (age)")
    return engine


def timed_ms(engine: Engine, sql: str, repeat: int = 7) -> float:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        engine.execute(sql)
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples)


def plan_kind(engine: Engine, sql: str) -> str:
    explain_sql = "AAYUSH EXPLAIN " + sql[len("AAYUSH ") :]
    text = engine.execute(explain_sql)["message"]
    if "INDEX SCAN" in text:
        return "INDEX SCAN"
    return "TABLE SCAN"


def rows_scanned(engine: Engine, sql: str) -> int:
    explain_sql = "AAYUSH EXPLAIN ANALYZE " + sql[len("AAYUSH ") :]
    text = engine.execute(explain_sql)["message"]
    for line in text.splitlines():
        if "ROWS SCANNED:" in line:
            return int(line.split(":", 1)[1])
    return -1


def format_row(cells: list[str]) -> str:
    return " | ".join(cells)


def run(sizes: list[int]) -> None:
    print(f"AayushSQL {__version__} index benchmarks")
    print(
        f"environment: {platform.system()} {platform.release()}, "
        f"Python {platform.python_version()}, cpu_count={os.cpu_count()}"
    )
    print()

    header = ["dataset", "operation", "plan", "rows_scanned", "median_ms", "with_index_ms"]
    print(format_row(header))
    print("-" * len(format_row(header)))

    for rows in sizes:
        plain = build_engine(rows, with_index=False)
        indexed = build_engine(rows, with_index=True)
        probe = rows // 2

        equal_sql = f"AAYUSH SELECT * FROM bench WHERE age = {probe}"
        range_sql = f"AAYUSH SELECT * FROM bench WHERE age >= {rows - 100}"
        wide_sql = f"AAYUSH SELECT * FROM bench WHERE age >= {probe}"
        for label, sql in (("equality", equal_sql), ("range(100)", range_sql), ("range(half)", wide_sql)):
            plain_ms = timed_ms(plain, sql)
            indexed_ms = timed_ms(indexed, sql)
            print(
                format_row(
                    [
                        str(rows),
                        label,
                        plan_kind(indexed, sql),
                        str(rows_scanned(indexed, sql)),
                        f"{plain_ms:.3f}",
                        f"{indexed_ms:.3f}",
                    ]
                )
            )

    print()
    print("index build cost")
    print("dataset | build_ms")
    print("------------------")
    for rows in sizes:
        started = time.perf_counter()
        engine = build_engine(rows, with_index=False)
        build_engine_ms = time.perf_counter()
        engine.execute("AAYUSH CREATE INDEX idx_bench_age ON bench (age)")
        finished = time.perf_counter()
        print(f"{rows} | {(finished - build_engine_ms) * 1000.0:.3f} (build_rows_ms={(build_engine_ms - started) * 1000.0:.1f})")

    print()
    print("mutation overhead (10k rows, 200 statements, ms/op)")
    print("operation | no_index | with_index")
    print("----------------------------------")
    mutations = 200
    operations = {
        "insert": ("AAYUSH INSERT INTO bench VALUES ({next}, {next}, 'new')", None),
        "update(indexed col)": (None, "AAYUSH UPDATE bench SET name = 'upd' WHERE age = {probe}"),
        "update(other col)": (None, "AAYUSH UPDATE bench SET age = age WHERE name = 'user{probe}'"),
        "delete(indexed col)": (None, "AAYUSH DELETE FROM bench WHERE age = {probe}"),
    }
    plain = build_engine(10_000, with_index=False)
    indexed = build_engine(10_000, with_index=True)
    next_id = 10_000
    for label, (insert_sql, other_sql) in operations.items():
        timings = {}
        for engine in (plain, indexed):
            started = time.perf_counter()
            for i in range(mutations):
                if insert_sql is not None:
                    engine.execute(insert_sql.format(next=next_id))
                    next_id += 1
                else:
                    engine.execute(other_sql.format(probe=5_000 + i))
            timings[id(engine)] = (time.perf_counter() - started) * 1000.0 / mutations
        print(f"{label} | {timings[id(plain)]:.4f} | {timings[id(indexed)]:.4f}")

    print()
    print("storage size (10k rows, .aaydb bytes)")
    print("no_index | with_index")
    print("---------------------")
    sizes_bytes = []
    benchmark_dir = Path(__file__).resolve().parent
    for with_index in (False, True):
        path = benchmark_dir / f".bench_storage_{int(with_index)}.aaydb"
        if path.exists():
            path.unlink()
        engine = Engine(path)
        engine.execute("AAYUSH CREATE TABLE bench (id INT, age INT, name TEXT)")
        for start in range(0, 10_000, CHUNK):
            stop = min(start + CHUNK, 10_000)
            values = ", ".join(f"({i}, {i}, 'user{i}')" for i in range(start, stop))
            engine.execute(f"AAYUSH INSERT INTO bench VALUES {values}")
        if with_index:
            engine.execute("AAYUSH CREATE INDEX idx_bench_age ON bench (age)")
        engine.close()
        sizes_bytes.append(path.stat().st_size)
        path.unlink()
    print(f"{sizes_bytes[0]} | {sizes_bytes[1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default="1000,10000,100000",
        help="Comma-separated row counts (default: 1000,10000,100000)",
    )
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    run(sizes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
