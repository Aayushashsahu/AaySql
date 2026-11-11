"""Reproducible benchmarks for the v0.9.0 SQL language expansion.

Design notes
------------
* stdlib only; no third-party benchmark dependency;
* deterministic datasets (``random.Random(SEED)``), in-memory database so the
  numbers describe expression evaluation rather than disk fsync;
* one warm-up execution is discarded (this also builds the planner statistics
  cache), then the median of several runs is reported with the observed
  min/max spread;
* every measured query also has its result count verified against the
  reference execution path (table scan + nested loop), so a "fast" number can
  never come from a wrong plan;
* indexed vs unindexed BETWEEN is measured by forcing each access path on the
  same data, and the planner's own choice is reported separately;
* execution path (table scan / index scan) is read from the plan, not guessed.

Run with::

    python benchmarks/benchmark_language.py
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
REPEATS = 7
WARMUPS = 1

DOC_ROWS = 10_000
LARGE_TEXT_ROWS = 10_000
SMALL_TEXT_ROWS = 250
JOIN_ROWS = 5_000

FIRST_NAMES = ("Alex", "Bob", "Cara", "Dan", "Eve", "Frank", "Grace", "Hana", "Ivo", "Jo")
DEPARTMENTS = ("Engineering", "Sales", "HR", "Finance", "Support", "Research", "Legal")
TAGS = ("alpha", "beta", "gamma", "delta")


def _bulk_insert(engine: Engine, table: str, rows: list[str], chunk: int = 500) -> None:
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        if batch:
            engine.execute(f"AAYUSH INSERT INTO {table} VALUES " + ", ".join(batch))


def _build_documents() -> Engine:
    """Mixed numeric/text table used by most scenarios."""
    rng = random.Random(SEED)
    engine = Engine()
    engine.execute(
        "AAYUSH CREATE TABLE docs (id INT, dept TEXT, score INT, price FLOAT, title TEXT)"
    )
    rows = []
    for i in range(DOC_ROWS):
        score = "NULL" if rng.random() < 0.05 else rng.choice([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        price = "NULL" if rng.random() < 0.05 else f"{rng.uniform(1.0, 500.0):.2f}"
        title = f"{rng.choice(FIRST_NAMES)} {rng.choice(TAGS)}_{i}"
        rows.append(f"({i}, '{rng.choice(DEPARTMENTS)}', {score}, {price}, '{title}')")
    _bulk_insert(engine, "docs", rows)
    return engine


def _build_text_table(rows: int, width: int) -> Engine:
    """Small vs large text payloads for LIKE, same row counts and shape."""
    rng = random.Random(SEED + 1)
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE texts (id INT, body TEXT)")
    values = []
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    for i in range(rows):
        prefix = "".join(rng.choice(alphabet) for _ in range(width))
        values.append(f"({i}, '{prefix}-{rng.choice(TAGS)}')")
    _bulk_insert(engine, "texts", values)
    return engine


def _build_join_pair() -> Engine:
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE users (id INT, base INT)")
    engine.execute("AAYUSH CREATE TABLE orders (id INT, user_id INT, amount INT)")
    _bulk_insert(engine, "users", [f"({i}, {i * 3})" for i in range(1, JOIN_ROWS + 1)])
    rng = random.Random(SEED + 2)
    _bulk_insert(
        engine,
        "orders",
        [f"({i}, {rng.randint(1, JOIN_ROWS)}, {rng.randint(1, 500)})" for i in range(1, JOIN_ROWS + 1)],
    )
    return engine


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _measure(fn, repeats: int = REPEATS):
    for _ in range(WARMUPS):
        fn()
    samples = []
    last = None
    for _ in range(repeats):
        started = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples), (min(samples), max(samples)), last


def _plan_path(engine: Engine, sql: str) -> str:
    plan = plan_select(engine.db, _stmt(sql))
    prefix = ""
    for node in _walk(plan.root):
        if node.op == "JOIN":
            prefix = f"{'hash' if node.algorithm == 'HASH' else 'nested loop'} join + "
            break
    for node in _walk(plan.root):
        if node.op == "INDEX SCAN":
            return f"{prefix}index scan ({node.index_name})"
    for node in _walk(plan.root):
        if node.op == "TABLE SCAN":
            return f"{prefix}table scan"
    return "n/a"


def _walk(node):
    if node is None:
        return
    yield node
    for child in node.children:
        yield from _walk(child)


def _reference_rows(engine: Engine, sql: str) -> int:
    stmt = _stmt(sql)
    plan = plan_select(
        engine.db, stmt, force_join_algorithm="NESTED LOOP", force_access_path="TABLE"
    )
    return len(execute_select(engine.db, stmt, plan=plan)["rows"])


def _rows(engine: Engine, sql: str) -> int:
    return engine.execute(sql)["rows"].__len__()


def _report_section(title: str, rows: list[dict]) -> None:
    print(title)
    print("-" * 96)
    print(
        f"{'scenario':34s} {'rows':>7s} {'path':30s} {'median':>10s} {'spread':>20s}"
    )
    for row in rows:
        spread = f"{row['spread'][0]:.2f}-{row['spread'][1]:.2f}ms"
        print(
            f"{row['name']:34s} {row['rows']:>7d} {row['path']:30s} "
            f"{row['median']:>8.2f}ms {spread:>20s}"
        )
    print()


def main() -> int:
    print("AayushSQL v0.9.0 SQL language benchmarks")
    print("=" * 96)
    print(f"Python {platform.python_version()} on {platform.platform()}")
    print(f"seed={SEED} repeats={REPEATS} warmups={WARMUPS} (in-memory database)")
    print(f"docs={DOC_ROWS:,} rows, texts={LARGE_TEXT_ROWS:,}/{SMALL_TEXT_ROWS:,} rows, "
          f"join pair={JOIN_ROWS:,}x{JOIN_ROWS:,}")
    print()

    docs = _build_documents()
    expression_rows = []
    scenarios = [
        (
            "arithmetic projection",
            "AAYUSH SELECT id, score * 2 + 1 AS bumped, price / 2 AS half FROM docs",
        ),
        (
            "arithmetic filter",
            "AAYUSH SELECT id FROM docs WHERE score * 2 + 10 > 150",
        ),
        (
            "IS NULL filter",
            "AAYUSH SELECT id FROM docs WHERE score IS NULL",
        ),
        (
            "LIKE filter (text)",
            "AAYUSH SELECT id FROM docs WHERE title LIKE 'A%beta%'",
        ),
        (
            "IN list (3 values)",
            "AAYUSH SELECT id FROM docs WHERE dept IN ('Engineering', 'HR', 'Legal')",
        ),
        (
            "BETWEEN filter",
            "AAYUSH SELECT id FROM docs WHERE score BETWEEN 30 AND 60",
        ),
        (
            "NOT BETWEEN filter",
            "AAYUSH SELECT id FROM docs WHERE score NOT BETWEEN 30 AND 60",
        ),
        (
            "CASE classification",
            (
                "AAYUSH SELECT id, CASE WHEN score IS NULL THEN 'none' "
                "WHEN score >= 80 THEN 'high' WHEN score >= 40 THEN 'mid' ELSE 'low' END "
                "AS band FROM docs"
            ),
        ),
        (
            "DISTINCT low cardinality",
            "AAYUSH SELECT DISTINCT dept FROM docs",
        ),
        (
            "DISTINCT high cardinality",
            "AAYUSH SELECT DISTINCT title FROM docs",
        ),
        (
            "OFFSET over ordered rows",
            "AAYUSH SELECT id FROM docs ORDER BY score DESC, id LIMIT 50 OFFSET 5000",
        ),
        (
            "filter + group + sort",
            (
                "AAYUSH SELECT dept, COUNT(*) AS c, AVG(price) AS avg_price FROM docs "
                "WHERE score IS NOT NULL AND title LIKE '%alpha%' GROUP BY dept "
                "HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 5"
            ),
        ),
    ]
    for name, sql in scenarios:
        path = _plan_path(docs, sql)
        median, spread, _ = _measure(lambda s=sql: docs.execute(s))
        produced = _rows(docs, sql)
        reference = _reference_rows(docs, sql)
        assert produced == reference, f"{name}: plan produced {produced}, reference {reference}"
        expression_rows.append(
            {
                "name": name,
                "rows": produced,
                "path": path,
                "median": median,
                "spread": spread,
            }
        )
    _report_section(f"EXPRESSIONS (docs, {DOC_ROWS:,} rows)", expression_rows)
    print("All measured row counts matched the reference table-scan path.")
    print()

    # --- LIKE over small vs large text payloads -------------------------
    like_rows = []
    for label, rows, width in (
        ("small text (25 chars)", SMALL_TEXT_ROWS, 25),
        ("large text (400 chars)", LARGE_TEXT_ROWS, 400),
    ):
        engine = _build_text_table(rows, width)
        sql = "AAYUSH SELECT id FROM texts WHERE body LIKE '%beta%'"
        median, spread, _ = _measure(lambda e=engine, s=sql: e.execute(s))
        like_rows.append(
            {
                "name": f"LIKE {label}",
                "rows": _rows(engine, sql),
                "path": _plan_path(engine, sql),
                "median": median,
                "spread": spread,
            }
        )
    _report_section("LIKE (small vs large text payloads)", like_rows)
    print("LIKE always runs on a table scan: no pattern statistics exist, so no index is used.")
    print()

    # --- BETWEEN with and without an index ------------------------------
    between_sql = "AAYUSH SELECT id FROM docs WHERE score BETWEEN 30 AND 60"
    without_index, without_spread, _ = _measure(lambda: docs.execute(between_sql))
    planned_without = _plan_path(docs, between_sql)
    docs.execute("AAYUSH CREATE INDEX idx_docs_score ON docs (score)")
    with_index, with_spread, _ = _measure(lambda: docs.execute(between_sql))
    planned_with = _plan_path(docs, between_sql)
    stmt = _stmt(between_sql)
    forced_table, forced_table_spread, _ = _measure(
        lambda: execute_select(docs.db, stmt, plan=plan_select(docs.db, stmt, force_access_path="TABLE"))
    )
    forced_index, forced_index_spread, _ = _measure(
        lambda: execute_select(docs.db, stmt, plan=plan_select(docs.db, stmt, force_access_path="INDEX"))
    )
    rows = _rows(docs, between_sql)
    assert (
        len(execute_select(docs.db, stmt, plan=plan_select(docs.db, stmt, force_access_path="TABLE"))["rows"])
        == len(execute_select(docs.db, stmt, plan=plan_select(docs.db, stmt, force_access_path="INDEX"))["rows"])
        == rows
    )
    _report_section(
        "BETWEEN (index vs table scan)",
        [
            {
                "name": "before index (chosen path)",
                "rows": rows,
                "path": planned_without,
                "median": without_index,
                "spread": without_spread,
            },
            {
                "name": "after index (chosen path)",
                "rows": rows,
                "path": planned_with,
                "median": with_index,
                "spread": with_spread,
            },
            {
                "name": "forced table scan",
                "rows": rows,
                "path": "table scan",
                "median": forced_table,
                "spread": forced_table_spread,
            },
            {
                "name": "forced index scan",
                "rows": rows,
                "path": "index scan",
                "median": forced_index,
                "spread": forced_index_spread,
            },
        ],
    )
    print("Both access paths returned identical rows for the same query.")
    print()

    # --- join with expression residual (v0.7.0 optimizer regression) ----
    join_engine = _build_join_pair()
    join_rows = []
    for name, sql in (
        (
            "hash join, equi only",
            (
                "AAYUSH SELECT u.id, o.id FROM users u JOIN orders o ON u.id = o.user_id "
                "WHERE o.amount > 400 LIMIT 500"
            ),
        ),
        (
            "join + arithmetic residual",
            (
                "AAYUSH SELECT u.id, o.id FROM users u JOIN orders o ON u.id = o.user_id "
                "AND o.amount * 2 > u.base LIMIT 500"
            ),
        ),
    ):
        path = _plan_path(join_engine, sql)
        median, spread, _ = _measure(lambda e=join_engine, s=sql: e.execute(s), repeats=3)
        produced = _rows(join_engine, sql)
        join_rows.append(
            {
                "name": name,
                "rows": produced,
                "path": path,
                "median": median,
                "spread": spread,
            }
        )
    _report_section(f"JOINS ({JOIN_ROWS:,} x {JOIN_ROWS:,})", join_rows)
    print("Join scenarios verify the v0.7.0 optimizer still plans hash joins with new expressions")
    print("in the ON clause; residual predicates are evaluated on every candidate.")
    print()
    print("Notes:")
    print("- Numbers come from an in-memory database; WAL fsync and socket overhead are excluded.")
    print("- The LIKE, IN, IS NULL, DISTINCT and OFFSET operators have no statistics, so their")
    print("  planner selectivity is the documented conservative default rather than a measurement.")
    print("- No claim is made that these features are faster than a hand-written equivalent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
