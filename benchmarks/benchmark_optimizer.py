"""Reproducible benchmarks for the v0.7.0 cost-based optimizer and hash joins.

Design notes
------------
* stdlib only (plus this package); no third-party benchmark dependency;
* deterministic datasets (``random.Random(seed)``), so runs are comparable;
* one warm-up execution is discarded, then the median of several runs is
  reported together with the observed min/max spread;
* every row count is verified against the reference path when the reference
  path is cheap enough to run, so a "fast" plan can never be a wrong plan;
* the reference path is the v0.6.x implementation: forceable nested-loop joins
  over table scans (``force_join_algorithm``/``force_access_path``);
* the nested-loop reference is only *measured* while the comparison count stays
  modest. Larger runs are extrapolated from the measured rate and labelled as
  extrapolation -- never reported as a measurement.

Run with::

    python benchmarks/benchmark_optimizer.py
"""

from __future__ import annotations

import platform
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

SEED = 20261111
REPEATS = 5
REFERENCE_REPEATS = 3
WARMUPS = 1
# The reference nested-loop path evaluates roughly 270k comparisons/second, so
# only small joins are measured end-to-end; larger ones are extrapolated.
NL_COMPARISON_LIMIT = 1_100_000
THROUGHPUT_PROBE_ROWS = 500

JOIN_SCENARIOS = (
    ("1k x 1k", 1_000, 1_000, "uniform"),
    ("10k x 10k", 10_000, 10_000, "uniform"),
    ("100k x 10k", 100_000, 10_000, "uniform"),
    ("10k x 100k", 10_000, 100_000, "uniform"),
    ("10k x 10k high duplicate", 10_000, 10_000, "duplicate"),
    ("10k x 10k skewed", 10_000, 10_000, "skewed"),
    ("10k x 10k many unmatched", 10_000, 10_000, "unmatched"),
)


# --- dataset construction -------------------------------------------------


def _bulk_insert(engine: Engine, table: str, rows: list[str], chunk: int = 1_000) -> None:
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        if batch:
            engine.execute(f"AAYUSH INSERT INTO {table} VALUES " + ", ".join(batch))


def _right_keys(left_rows: int, right_rows: int, distribution: str, rng: random.Random) -> list[int]:
    if distribution == "uniform":
        return [rng.randint(1, left_rows) for _ in range(right_rows)]
    if distribution == "duplicate":
        # Only ten distinct keys: every key has hundreds of matches.
        return [rng.randint(1, 10) for _ in range(right_rows)]
    if distribution == "skewed":
        keys = []
        for _ in range(right_rows):
            if rng.random() < 0.8:
                keys.append(rng.randint(1, 25))
            else:
                keys.append(rng.randint(1, left_rows))
        return keys
    if distribution == "unmatched":
        # Most right rows reference keys that do not exist on the left.
        return [rng.randint(left_rows + 1, left_rows * 2) for _ in range(right_rows)]
    raise ValueError(distribution)


def build_join_dataset(left_rows: int, right_rows: int, distribution: str) -> Engine:
    rng = random.Random(SEED + left_rows * 31 + right_rows)
    engine = Engine()
    # No PRIMARY KEY on benchmark tables: the engine's per-row uniqueness check
    # is linear in table size, which would make dataset creation the bottleneck
    # instead of the query under test. Join performance does not depend on it.
    engine.execute("AAYUSH CREATE TABLE users (id INT, name TEXT, age INT)")
    engine.execute("AAYUSH CREATE TABLE orders (id INT, user_id INT, total INT)")
    _bulk_insert(
        engine,
        "users",
        [f"({i}, 'u{i}', {18 + i % 40})" for i in range(1, left_rows + 1)],
    )
    keys = _right_keys(left_rows, right_rows, distribution, rng)
    _bulk_insert(
        engine,
        "orders",
        [f"({j}, {key}, {j % 97})" for j, key in enumerate(keys, start=1)],
    )
    return engine


def build_scan_dataset(rows: int) -> Engine:
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT, k INT, v INT)")
    _bulk_insert(engine, "t", [f"({i}, {i}, {i % 7})" for i in range(1, rows + 1)])
    engine.execute("AAYUSH CREATE INDEX idx_t_k ON t (k)")
    return engine


# --- measurement ----------------------------------------------------------


def _time(engine: Engine, stmt, plan, repeats: int = REPEATS) -> tuple[float, float, float, int]:
    for _ in range(WARMUPS):
        execute_select(engine.db, stmt, plan=plan)
    times = []
    row_count = 0
    for _ in range(repeats):
        started = time.perf_counter()
        result = execute_select(engine.db, stmt, plan=plan)
        times.append((time.perf_counter() - started) * 1000.0)
        row_count = result["row_count"]
    return statistics.median(times), min(times), max(times), row_count


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _plan(engine: Engine, stmt, *, reference: bool):
    if reference:
        return plan_select(
            engine.db,
            stmt,
            force_join_algorithm="NESTED LOOP",
            force_access_path="TABLE",
        )
    return plan_select(engine.db, stmt)


def _describe(node) -> str:
    if node.op == "JOIN":
        text = f"{node.algorithm} {node.join_type}"
        for child in node.children:
            text += f" <- {_describe(child)}"
        return text
    if node.op in ("TABLE SCAN", "INDEX SCAN"):
        return node.op
    return _describe(node.children[0]) if node.children else node.op


def benchmark_joins() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    queries = (
        (
            "INNER",
            (
                "AAYUSH SELECT u.id, o.total FROM users u "
                "INNER JOIN orders o ON u.id = o.user_id"
            ),
        ),
        (
            "LEFT",
            (
                "AAYUSH SELECT u.id, o.total FROM users u "
                "LEFT JOIN orders o ON u.id = o.user_id"
            ),
        ),
    )
    for name, left_rows, right_rows, distribution in JOIN_SCENARIOS:
        engine = build_join_dataset(left_rows, right_rows, distribution)
        comparisons = left_rows * right_rows
        try:
            for join_type, sql in queries:
                stmt = _stmt(sql)
                plan = _plan(engine, stmt, reference=False)
                median, low, high, row_count = _time(engine, stmt, plan)
                entry: dict[str, object] = {
                    "scenario": name,
                    "join": join_type,
                    "rows": row_count,
                    "plan": _describe(plan.root),
                    "median_ms": median,
                    "min_ms": low,
                    "max_ms": high,
                    "reference_ms": None,
                    "reference_note": "",
                }
                if comparisons <= NL_COMPARISON_LIMIT:
                    ref_plan = _plan(engine, stmt, reference=True)
                    ref_median, ref_low, ref_high, ref_rows = _time(
                        engine, stmt, ref_plan, repeats=REFERENCE_REPEATS
                    )
                    assert ref_rows == row_count, (name, join_type, ref_rows, row_count)
                    entry["reference_ms"] = ref_median
                    entry["reference_spread"] = (ref_low, ref_high)
                else:
                    entry["reference_note"] = (
                        f"not measured ({comparisons:,} nested-loop comparisons); "
                        "extrapolated below"
                    )
                rows.append(entry)
        finally:
            engine.close()
    return rows


def benchmark_access_paths() -> list[dict[str, object]]:
    engine = build_scan_dataset(50_000)
    rows: list[dict[str, object]] = []
    queries = (
        ("narrow equality", "AAYUSH SELECT id FROM t WHERE k = 4242"),
        ("narrow range", "AAYUSH SELECT id FROM t WHERE k >= 49990"),
        ("half range", "AAYUSH SELECT id FROM t WHERE k >= 25000"),
        ("full range", "AAYUSH SELECT id FROM t WHERE k >= 0"),
    )
    try:
        for label, sql in queries:
            stmt = _stmt(sql)
            chosen = plan_select(engine.db, stmt)
            chosen_scan = next(
                node
                for node in _walk(chosen.root)
                if node.op in ("TABLE SCAN", "INDEX SCAN")
            )
            table_plan = plan_select(engine.db, stmt, force_access_path="TABLE")
            index_plan = plan_select(engine.db, stmt, force_access_path="INDEX")
            table_median, table_low, table_high, table_rows = _time(engine, stmt, table_plan)
            index_median, index_low, index_high, index_rows = _time(engine, stmt, index_plan)
            chosen_median, chosen_low, chosen_high, chosen_rows = _time(engine, stmt, chosen)
            assert table_rows == index_rows == chosen_rows
            rows.append(
                {
                    "label": label,
                    "chosen": chosen_scan.op,
                    "rows": chosen_rows,
                    "table_ms": table_median,
                    "table_spread": (table_low, table_high),
                    "index_ms": index_median,
                    "index_spread": (index_low, index_high),
                    "chosen_ms": chosen_median,
                    "chosen_spread": (chosen_low, chosen_high),
                }
            )
    finally:
        engine.close()
    return rows


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def measure_reference_rate() -> tuple[float, float, int]:
    """Measure how many nested-loop comparisons the reference path does per second."""
    engine = build_join_dataset(THROUGHPUT_PROBE_ROWS, THROUGHPUT_PROBE_ROWS, "uniform")
    try:
        sql = "AAYUSH SELECT u.id, o.total FROM users u INNER JOIN orders o ON u.id = o.user_id"
        stmt = _stmt(sql)
        plan = _plan(engine, stmt, reference=True)
        median, _, _, _ = _time(engine, stmt, plan, repeats=REFERENCE_REPEATS)
        comparisons = THROUGHPUT_PROBE_ROWS**2
        return comparisons / (median / 1000.0), median, comparisons
    finally:
        engine.close()


# --- reporting ------------------------------------------------------------


def main() -> int:
    print("AayushSQL v0.7.0 optimizer benchmarks")
    print("=" * 78)
    print(f"date            : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"python          : {platform.python_version()} ({platform.python_implementation()})")
    print(f"platform        : {platform.platform()}")
    print(f"processor       : {platform.processor() or 'unknown'}")
    print(f"seed            : {SEED}")
    print(f"repeats         : {REPEATS} (plus {WARMUPS} discarded warm-up)")
    print("timing          : median wall-clock per query, min/max spread")
    print("reference path  : forced nested-loop joins over table scans (v0.6.x)")
    print()

    join_rows = benchmark_joins()
    print("JOIN BENCHMARKS")
    print("-" * 78)
    header = f"{'scenario':28s} {'join':6s} {'rows':>7s} {'plan':22s} {'median':>9s} {'reference':>11s} {'speedup':>8s}"
    print(header)
    for row in join_rows:
        speedup = "-"
        if row["reference_ms"] is not None and row["median_ms"] > 0:
            speedup = f"{row['reference_ms'] / row['median_ms']:.1f}x"
        plan = str(row["plan"])
        if len(plan) > 22:
            plan = plan[:19] + "..."
        reference = "-" if row["reference_ms"] is None else f"{row['reference_ms']:.1f}ms"
        print(
            f"{row['scenario']:28s} {row['join']:6s} {row['rows']:>7d} {plan:22s} "
            f"{row['median_ms']:>7.1f}ms {reference:>11s} {speedup:>8s}"
        )
        print(
            f"    optimized spread {row['min_ms']:.1f}-{row['max_ms']:.1f}ms; "
            f"plan {row['plan']}"
        )
        if row["reference_note"]:
            print(f"    reference: {row['reference_note']}")
        elif row["reference_ms"] is not None:
            low, high = row["reference_spread"]
            print(f"    reference spread {low:.1f}-{high:.1f}ms")
    print()

    scan_rows = benchmark_access_paths()
    print("ACCESS PATH BENCHMARKS (table t: 50,000 rows, index on k)")
    print("-" * 78)
    print(
        f"{'predicate':18s} {'chosen':12s} {'rows':>7s} {'table scan':>12s} "
        f"{'index scan':>12s} {'chosen time':>12s}"
    )
    for row in scan_rows:
        print(
            f"{row['label']:18s} {row['chosen']:12s} {row['rows']:>7d} "
            f"{row['table_ms']:>10.2f}ms {row['index_ms']:>10.2f}ms "
            f"{row['chosen_ms']:>10.2f}ms"
        )
        print(
            f"    table spread {row['table_spread'][0]:.2f}-{row['table_spread'][1]:.2f}ms; "
            f"index spread {row['index_spread'][0]:.2f}-{row['index_spread'][1]:.2f}ms; "
            f"chosen spread {row['chosen_spread'][0]:.2f}-{row['chosen_spread'][1]:.2f}ms"
        )
    print()

    print("EXTRAPOLATION (not a measurement)")
    print("-" * 78)
    rate, probe_median, probe_comparisons = measure_reference_rate()
    print(
        f"measured nested-loop rate: ~{rate:,.0f} comparisons/second "
        f"({THROUGHPUT_PROBE_ROWS}x{THROUGHPUT_PROBE_ROWS} INNER, "
        f"{probe_comparisons:,} comparisons in {probe_median:.1f}ms)"
    )
    for row in join_rows:
        if row["reference_ms"] is not None:
            continue
        name = str(row["scenario"])
        left_rows, right_rows = next(
            (left, right) for label, left, right, _ in JOIN_SCENARIOS if label == name
        )
        estimated = left_rows * right_rows / rate
        print(
            f"  {name:28s} {left_rows * right_rows:>14,} comparisons "
            f"-> ~{estimated:,.0f}s reference (extrapolated)"
        )
    print()
    print("All optimized row counts were verified against the reference path")
    print("wherever the reference path was measured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
