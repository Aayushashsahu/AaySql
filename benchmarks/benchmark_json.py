"""Reproducible benchmarks for Phase 5 Native JSON & Document Model.

Design notes
------------
* stdlib only; deterministic dataset using random.Random(SEED);
* in-memory database to measure JSON processing throughput;
* measures:
  1. JSON parsing and document insertion throughput;
  2. Dot-navigation path extraction (SELECT profile.name, profile.role);
  3. Array bracket indexing (SELECT tags[0], tags[1]);
  4. Predicate evaluation on document properties (WHERE profile.score > 80);
  5. JSON functions (JSON_EXTRACT, JSON_EXISTS);
  6. Point mutation (UPDATE ... SET profile.role = 'lead');
  7. Copy-on-write deep copy isolation throughput.

Run with::

    python benchmarks/benchmark_json.py
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
from aaysql.json_value import (
    parse_json_document,
)

SEED = 20261114
NUM_DOCS = 5_000
REPEATS = 5
WARMUPS = 1


def _generate_documents(n: int) -> list[str]:
    rng = random.Random(SEED)
    roles = ["engineer", "admin", "analyst", "manager", "support", "designer"]
    cities = ["Bengaluru", "San Francisco", "Tokyo", "London", "Berlin", "Singapore"]
    all_tags = ["sql", "python", "db", "cloud", "backend", "frontend", "devops"]

    docs = []
    for i in range(1, n + 1):
        doc = {
            "id": i,
            "name": f"User_{i}",
            "role": rng.choice(roles),
            "score": rng.randint(50, 100),
            "address": {
                "city": rng.choice(cities),
                "zip": rng.randint(10000, 99999),
            },
            "tags": rng.sample(all_tags, k=3),
            "active": rng.choice([True, False]),
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


def main():
    print("=" * 70)
    print("AayushSQL 2.0 — Phase 5: Native JSON & Document Model Benchmark")
    print("=" * 70)

    docs = _generate_documents(NUM_DOCS)
    total_raw_bytes = sum(len(d.encode("utf-8")) for d in docs)
    print(f"Dataset: {NUM_DOCS:,} documents ({total_raw_bytes / (1024 * 1024):.2f} MB raw JSON)")

    # 1. Parsing & Validation
    def benchmark_parse():
        for d in docs:
            parse_json_document(d)

    med, lo, hi = _time_fn(benchmark_parse)
    docs_sec = NUM_DOCS / med
    mb_sec = (total_raw_bytes / (1024 * 1024)) / med
    print("\n1. Raw JSON Validation & Strict Parse:")
    print(f"   Median: {med*1000:.2f} ms (min: {lo*1000:.2f} ms, max: {hi*1000:.2f} ms)")
    print(f"   Throughput: {docs_sec:,.0f} docs/sec ({mb_sec:.2f} MB/sec)")

    # 2. Database Ingestion
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")

    def benchmark_insert():
        eng = Engine()
        eng.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, profile JSON);")
        chunk_size = 500
        for start in range(0, NUM_DOCS, chunk_size):
            chunk = docs[start : start + chunk_size]
            values = ", ".join(f"({start + idx + 1}, '{d}')" for idx, d in enumerate(chunk))
            eng.execute(f"AAYUSH INSERT INTO users (id, profile) VALUES {values};")

    med, lo, hi = _time_fn(benchmark_insert, repeats=3, warmups=1)
    print("\n2. SQL Table Document Ingestion:")
    print(f"   Median: {med*1000:.2f} ms | Throughput: {NUM_DOCS / med:,.0f} rows/sec")

    # Populate main engine
    chunk_size = 500
    for start in range(0, NUM_DOCS, chunk_size):
        chunk = docs[start : start + chunk_size]
        values = ", ".join(f"({start + idx + 1}, '{d}')" for idx, d in enumerate(chunk))
        engine.execute(f"AAYUSH INSERT INTO users (id, profile) VALUES {values};")

    # 3. Path Extraction (Dot Navigation)
    def benchmark_dot_navigation():
        res = engine.execute("AAYUSH SELECT id, profile.name, profile.role, profile.address.city FROM users;")
        return len(res["rows"])

    med, lo, hi = _time_fn(benchmark_dot_navigation)
    print("\n3. Dot Navigation Projection (profile.name, role, address.city):")
    print(f"   Median: {med*1000:.2f} ms | Throughput: {NUM_DOCS / med:,.0f} rows/sec")

    # 4. Bracket Indexing
    def benchmark_bracket_access():
        res = engine.execute("AAYUSH SELECT id, profile.tags[0], profile.tags[1] FROM users;")
        return len(res["rows"])

    med, lo, hi = _time_fn(benchmark_bracket_access)
    print("\n4. Bracket Array Indexing (profile.tags[0], tags[1]):")
    print(f"   Median: {med*1000:.2f} ms | Throughput: {NUM_DOCS / med:,.0f} rows/sec")

    # 5. Predicate Filtering on JSON Path
    def benchmark_predicate():
        res = engine.execute("AAYUSH SELECT id, profile.name FROM users WHERE profile.score >= 90;")
        return len(res["rows"])

    med, lo, hi = _time_fn(benchmark_predicate)
    res = engine.execute("AAYUSH SELECT id FROM users WHERE profile.score >= 90;")
    print("\n5. Where Predicate Evaluation (profile.score >= 90):")
    print(f"   Matched: {len(res['rows']):,} rows")
    print(f"   Median: {med*1000:.2f} ms | Throughput: {NUM_DOCS / med:,.0f} rows/sec")

    # 6. JSON Functions (JSON_EXTRACT & JSON_EXISTS)
    def benchmark_json_extract():
        res = engine.execute("AAYUSH SELECT JSON_EXTRACT(profile, '$.address.city') AS city FROM users;")
        return len(res["rows"])

    med, lo, hi = _time_fn(benchmark_json_extract)
    print("\n6. Function JSON_EXTRACT(profile, '$.address.city'):")
    print(f"   Median: {med*1000:.2f} ms | Throughput: {NUM_DOCS / med:,.0f} rows/sec")

    # 7. Point Mutation (UPDATE ... SET profile.role = 'lead')
    def benchmark_point_mutation():
        engine.execute("AAYUSH UPDATE users SET profile.role = 'lead' WHERE id <= 500;")

    med, lo, hi = _time_fn(benchmark_point_mutation)
    print("\n7. Point Mutation (UPDATE 500 rows SET profile.role = 'lead'):")
    print(f"   Median: {med*1000:.2f} ms | Latency per row: {(med * 1000) / 500:.3f} ms")

    print("\n" + "=" * 70)
    print("Benchmark completed successfully.")
    print("=" * 70)


if __name__ == "__main__":
    main()
