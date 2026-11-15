"""Reproducible benchmarks for Phase 6 Scalar JSON Path Indexes & Cross-Model Joins.

Evaluates:
1. Ingestion throughput with and without scalar path index (mutation overhead).
2. Point equality lookup: Table Scan vs Path Index Scan (Performance Gate G7: >= 20x speedup).
3. Range scan lookup: Table Scan vs Path Index Scan.
4. Index creation build time over existing documents.
5. Cross-model Join: Nested-loop vs Hash Join with JSON path extraction.

Run with:
    python benchmarks/benchmark_path_indexes.py
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import plan_select

SEED = 20261115
NUM_DOCS = 10_000
REPEATS = 5
WARMUPS = 1


def _generate_documents(n: int) -> list[str]:
    rng = random.Random(SEED)
    cities = ["Bengaluru", "San Francisco", "Tokyo", "London", "Berlin", "Singapore", "New York", "Paris"]
    tiers = ["bronze", "silver", "gold", "platinum"]

    docs = []
    for i in range(1, n + 1):
        doc = {
            "account": {
                "id": i,
                "tier": rng.choice(tiers),
            },
            "score": rng.randint(0, 1000),
            "address": {
                "city": rng.choice(cities),
                "zip": rng.randint(10000, 99999),
            },
        }
        docs.append(json.dumps(doc))
    return docs


def _time_fn(fn, repeats=REPEATS, warmups=WARMUPS) -> tuple[float, float, float]:
    for _ in range(warmups):
        fn()
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples), min(samples), max(samples)


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _run_forced_table_scan(engine: Engine, sql: str):
    stmt = _stmt(sql)
    plan = plan_select(engine.db, stmt, force_access_path="TABLE")
    return execute_select(engine.db, stmt, plan=plan)


def main():
    print("=" * 75)
    print("AayushSQL 2.0 — Phase 6: Scalar JSON Path Indexes & Cross-Model Benchmark")
    print("=" * 75)

    docs = _generate_documents(NUM_DOCS)
    print(f"Dataset: {NUM_DOCS:,} documents generated deterministically (SEED={SEED})")

    # 1. Ingestion without index
    engine_no_idx = Engine()
    engine_no_idx.execute("AAYUSH CREATE COLLECTION customers;")
    t0 = time.perf_counter()
    chunk_size = 500
    for start in range(0, NUM_DOCS, chunk_size):
        chunk = docs[start : start + chunk_size]
        vals = ", ".join(f"('{d}')" for d in chunk)
        engine_no_idx.execute(f"AAYUSH INSERT INTO customers (body) VALUES {vals};")
    t_insert_no_idx = time.perf_counter() - t0
    print("\n1. Ingestion without Index (10k docs):")
    print(f"   Time: {t_insert_no_idx*1000:.2f} ms ({NUM_DOCS / t_insert_no_idx:,.0f} docs/sec)")

    # 2. Ingestion with path index
    engine_with_idx = Engine()
    engine_with_idx.execute("AAYUSH CREATE COLLECTION customers;")
    engine_with_idx.execute("AAYUSH CREATE INDEX idx_acc_id ON customers (body.account.id);")
    engine_with_idx.execute("AAYUSH CREATE INDEX idx_score ON customers (body.score);")

    t0 = time.perf_counter()
    for start in range(0, NUM_DOCS, chunk_size):
        chunk = docs[start : start + chunk_size]
        vals = ", ".join(f"('{d}')" for d in chunk)
        engine_with_idx.execute(f"AAYUSH INSERT INTO customers (body) VALUES {vals};")
    t_insert_with_idx = time.perf_counter() - t0
    overhead = ((t_insert_with_idx - t_insert_no_idx) / t_insert_no_idx) * 100.0
    print("\n2. Ingestion with 2 Path Indexes (10k docs):")
    print(f"   Time: {t_insert_with_idx*1000:.2f} ms ({NUM_DOCS / t_insert_with_idx:,.0f} docs/sec)")
    print(f"   Index maintenance overhead: +{overhead:.1f}%")

    # 3. Point Equality Lookup (Performance Gate G7)
    target_id = 7342
    sql_point = f"AAYUSH SELECT * FROM customers WHERE body.account.id = {target_id};"

    def bench_table_scan():
        _run_forced_table_scan(engine_with_idx, sql_point)

    def bench_index_scan():
        engine_with_idx.execute(sql_point)

    med_ts, _, _ = _time_fn(bench_table_scan, repeats=10, warmups=2)
    med_idx, _, _ = _time_fn(bench_index_scan, repeats=10, warmups=2)
    speedup = med_ts / med_idx if med_idx > 0 else float("inf")

    print(f"\n3. Point Equality Lookup (WHERE body.account.id = {target_id}):")
    print(f"   Table Scan median: {med_ts*1000:.3f} ms")
    print(f"   Index Scan median: {med_idx*1000:.3f} ms")
    print(f"   Speedup: {speedup:.1f}x (Gate G7 Target: >= 20.0x)")
    gate_g7_passed = speedup >= 20.0
    print(f"   Status: {'[PASSED]' if gate_g7_passed else '[FAILED]'}")

    # 4. Range Scan Lookup (WHERE body.score BETWEEN 500 AND 510)
    sql_range = "AAYUSH SELECT * FROM customers WHERE body.score BETWEEN 500 AND 510;"

    def bench_range_table():
        _run_forced_table_scan(engine_with_idx, sql_range)

    def bench_range_idx():
        engine_with_idx.execute(sql_range)

    med_r_ts, _, _ = _time_fn(bench_range_table, repeats=10, warmups=2)
    med_r_idx, _, _ = _time_fn(bench_range_idx, repeats=10, warmups=2)
    range_speedup = med_r_ts / med_r_idx if med_r_idx > 0 else float("inf")

    print("\n4. Range Scan Lookup (WHERE body.score BETWEEN 500 AND 510):")
    print(f"   Table Scan median: {med_r_ts*1000:.3f} ms")
    print(f"   Index Scan median: {med_r_idx*1000:.3f} ms")
    print(f"   Speedup: {range_speedup:.1f}x")

    # 5. Build Index over Pre-existing Data
    def bench_create_index():
        engine_no_idx.execute("AAYUSH CREATE INDEX idx_temp ON customers (body.address.zip);")
        engine_no_idx.execute("AAYUSH DROP INDEX idx_temp;")

    med_build, _, _ = _time_fn(bench_create_index, repeats=3, warmups=1)
    print("\n5. Index Build Time over 10k Documents:")
    print(f"   Median build time: {med_build*1000:.2f} ms")

    # 6. Cross-Model Relational / Document Join
    engine_join = Engine()
    engine_join.execute("AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, active BOOL);")
    engine_join.execute("AAYUSH CREATE COLLECTION audits;")

    for i in range(1, 1001):
        engine_join.execute(f"AAYUSH INSERT INTO accounts VALUES ({i}, true);")

    audit_chunk = 500
    for start in range(0, 2000, audit_chunk):
        vals = ", ".join(f"('{{\"acc_id\": {(start + idx) % 500 + 1}, \"action\": \"audit\"}}')" for idx in range(audit_chunk))
        engine_join.execute(f"AAYUSH INSERT INTO audits (body) VALUES {vals};")

    sql_join = "AAYUSH SELECT a.id, au.body.action FROM accounts a JOIN audits au ON a.id = au.body.acc_id;"

    def bench_join_nl():
        stmt = _stmt(sql_join)
        plan = plan_select(engine_join.db, stmt, force_join_algorithm="NESTED LOOP")
        execute_select(engine_join.db, stmt, plan=plan)

    def bench_join_hash():
        stmt = _stmt(sql_join)
        plan = plan_select(engine_join.db, stmt, force_join_algorithm="HASH")
        execute_select(engine_join.db, stmt, plan=plan)

    med_nl, _, _ = _time_fn(bench_join_nl, repeats=3, warmups=1)
    med_hash, _, _ = _time_fn(bench_join_hash, repeats=5, warmups=1)
    join_speedup = med_nl / med_hash if med_hash > 0 else float("inf")

    print("\n6. Cross-Model Join (1k accounts x 2k audits):")
    print(f"   Nested-Loop Join: {med_nl*1000:.2f} ms")
    print(f"   Hash Join with Path Extraction: {med_hash*1000:.2f} ms")
    print(f"   Speedup: {join_speedup:.1f}x")

    print("\n" + "=" * 75)
    print("Benchmark complete.")
    print("=" * 75)


if __name__ == "__main__":
    main()
