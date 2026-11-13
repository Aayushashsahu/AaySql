"""Query-level tests for the v0.9.0 language expansion.

Checks the new expressions as they interact with the rest of the engine: query
planning and EXPLAIN, predicate pushdown around joins, hash-join/nested-loop
equivalence, indexed and unindexed execution producing identical rows, and
persistence of statements that use the new syntax.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import plan_select


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _reference(engine: Engine, sql: str):
    stmt = _stmt(sql)
    plan = plan_select(
        engine.db, stmt, force_join_algorithm="NESTED LOOP", force_access_path="TABLE"
    )
    return execute_select(engine.db, stmt, plan=plan)


def _indexed(engine: Engine, sql: str):
    stmt = _stmt(sql)
    plan = plan_select(engine.db, stmt, force_access_path="INDEX")
    return execute_select(engine.db, stmt, plan=plan)


def _rows(engine: Engine, sql: str):
    return engine.execute(sql)["rows"]


@pytest.fixture()
def employees():
    e = Engine()
    e.execute(
        "AAYUSH CREATE TABLE employees (id INT, name TEXT, department TEXT, salary INT, score FLOAT)"
    )
    e.execute(
        """
        AAYUSH INSERT INTO employees VALUES
            (1, 'Alex',  'Engineering', 80000, 91.5),
            (2, 'Bob',   'Sales',       45000, 78.0),
            (3, 'Cara',  'Engineering', 61000, 60.0),
            (4, 'Dan',   NULL,          30000, NULL),
            (5, 'Eve',   'HR',          30000, 55.5),
            (6, 'Frank', 'Sales',       NULL,  88.0)
        """
    )
    return e


# --- planner: EXPLAIN and estimates --------------------------------------


def test_explain_is_still_non_executing_for_new_expressions(employees):
    before = _rows(employees, "AAYUSH SELECT * FROM employees")
    plan = employees.execute(
        "AAYUSH EXPLAIN SELECT id FROM employees WHERE salary BETWEEN 1 AND 2 AND name LIKE 'A%'"
    )["message"]
    assert "QUERY PLAN" in plan
    assert "BETWEEN" in plan
    assert "LIKE" in plan
    assert _rows(employees, "AAYUSH SELECT * FROM employees") == before
    # A plan must never divide by zero: it does not evaluate the expression.
    assert "QUERY PLAN" in employees.execute("AAYUSH EXPLAIN SELECT 1 / 0 AS r FROM employees")["message"]
    assert "QUERY PLAN" in employees.execute(
        "AAYUSH EXPLAIN SELECT name FROM employees WHERE 1 / 0 = 1"
    )["message"]


def test_explain_renders_the_new_expression_syntax(employees):
    plan = employees.execute(
        "AAYUSH EXPLAIN SELECT id FROM employees WHERE "
        "salary BETWEEN 30000 AND 60000 AND name NOT LIKE '%x' AND id IN (1, 2, 3) "
        "AND department IS NOT NULL"
    )["message"]
    assert "salary BETWEEN 30000 AND 60000" in plan
    assert "name NOT LIKE '%x'" in plan
    assert "id IN (1, 2, 3)" in plan
    assert "department IS NOT NULL" in plan


def test_explain_renders_case_and_arithmetic_in_projection(employees):
    plan = employees.execute(
        "AAYUSH EXPLAIN SELECT CASE WHEN salary > 50000 THEN salary * 1.1 ELSE salary END AS adjusted "
        "FROM employees"
    )["message"]
    assert "CASE" in plan and "END" in plan
    assert "salary * 1.1" in plan
    assert "adjusted" in plan


def test_explain_shows_distinct_offset_and_limit(employees):
    plan = employees.execute(
        "AAYUSH EXPLAIN SELECT DISTINCT department FROM employees ORDER BY department LIMIT 2 OFFSET 1"
    )["message"]
    assert "DISTINCT" in plan
    assert "OFFSET: 1" in plan
    assert "LIMIT: 2" in plan


def test_explain_analyze_reports_offset_and_limit_measurements(employees):
    result = employees.execute(
        "AAYUSH EXPLAIN ANALYZE SELECT DISTINCT department FROM employees "
        "ORDER BY department LIMIT 2 OFFSET 1"
    )["message"]
    assert "DISTINCT: rows_in=6 rows_out=4 duplicates_removed=2" in result
    assert "OFFSET: discarded=1 rows_in=4 rows_out=3" in result
    assert "LIMIT: rows_in=3 rows_out=2" in result
    assert "EXECUTION TIME:" in result


def test_explain_analyze_does_not_fabricate_operator_counts(employees):
    result = employees.execute(
        "AAYUSH EXPLAIN ANALYZE SELECT id FROM employees WHERE salary BETWEEN 1 AND 2"
    )["message"]
    assert "FILTER: salary BETWEEN 1 AND 2" in result
    assert "rows_out=0" in result
    assert "ROWS RETURNED: 0" in result


# --- index integration ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "AAYUSH SELECT id, salary FROM employees WHERE salary BETWEEN 30000 AND 60000 ORDER BY id",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000 ORDER BY salary DESC",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000 ORDER BY id LIMIT 2 OFFSET 1",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000 AND name LIKE 'A%'",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000 AND id IN (1, 3, 4)",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000 AND score IS NOT NULL",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 60000 AND 30000",
        "AAYUSH SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000 AND department = 'Sales'",
    ],
)
def test_between_index_scan_matches_table_scan(employees, sql):
    employees.execute("AAYUSH CREATE INDEX idx_salary ON employees (salary)")
    indexed = _indexed(employees, sql)
    reference = _reference(employees, sql)
    assert indexed == reference


def test_between_uses_an_index_only_when_it_pays(employees):
    employees.execute("AAYUSH CREATE INDEX idx_salary ON employees (salary)")
    plan = employees.execute(
        "AAYUSH EXPLAIN SELECT id FROM employees WHERE salary BETWEEN 30000 AND 60000"
    )["message"]
    assert "INDEX SCAN: idx_salary" in plan
    assert "CONDITION: salary BETWEEN 30000 AND 60000" in plan


def test_is_null_and_like_and_in_do_not_use_an_index(employees):
    employees.execute("AAYUSH CREATE INDEX idx_salary ON employees (salary)")
    employees.execute("AAYUSH CREATE INDEX idx_name ON employees (name)")
    for predicate in ("salary IS NULL", "name LIKE 'A%'", "id IN (1, 2)", "salary IS NOT NULL"):
        plan = employees.execute(
            f"AAYUSH EXPLAIN SELECT id FROM employees WHERE {predicate}"
        )["message"]
        assert "INDEX SCAN" not in plan, predicate


# --- predicate placement and join equivalence ----------------------------


def test_left_join_null_extension_survives_is_null_pushed_correctly():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE u (id INT, name TEXT)")
    e.execute("AAYUSH CREATE TABLE o (id INT, user_id INT, amount INT)")
    e.execute("AAYUSH INSERT INTO u VALUES (1, 'Alex'), (2, 'Bob'), (3, 'Cara')")
    e.execute("AAYUSH INSERT INTO o VALUES (10, 1, 100), (11, 1, 20), (12, 2, 75)")
    sql = (
        "AAYUSH SELECT u.id, o.id FROM u LEFT JOIN o ON u.id = o.user_id "
        "WHERE o.id IS NULL OR o.amount * 2 > 100 ORDER BY u.id, o.id"
    )
    assert _rows(e, sql) == _reference(e, sql)["rows"] == [
        {"u.id": 1, "o.id": 10},
        {"u.id": 2, "o.id": 12},
        {"u.id": 3, "o.id": None},
    ]


def test_predicate_over_nullable_side_is_not_pushed_before_a_left_join():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE u (id INT)")
    e.execute("AAYUSH CREATE TABLE o (id INT, user_id INT, tag TEXT)")
    e.execute("AAYUSH INSERT INTO u VALUES (1), (2)")
    e.execute("AAYUSH INSERT INTO o VALUES (10, 1, 'keep'), (11, 1, NULL)")
    plan = plan_select(
        e.db, _stmt("AAYUSH SELECT u.id FROM u LEFT JOIN o ON u.id = o.user_id WHERE o.tag IS NULL")
    )
    assert any(node.op == "FILTER" for node in _walk(plan.root)), "expected a post-join FILTER"
    right_scan = next(
        node for node in _walk(plan.root) if node.op in ("TABLE SCAN", "INDEX SCAN") and node.ref == "o"
    )
    assert "tag IS NULL" not in (right_scan.predicate_text or "")


def test_case_and_like_in_join_on_are_evaluated_per_candidate():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE a (id INT, kind TEXT, minimum INT)")
    e.execute("AAYUSH CREATE TABLE b (id INT, user_id INT, amount INT, tag TEXT)")
    e.execute("AAYUSH INSERT INTO a VALUES (1, 'strict', 50), (2, 'loose', 0)")
    e.execute("AAYUSH INSERT INTO b VALUES (10, 1, 100, 'good'), (11, 1, 10, 'bad'), (12, 2, 10, 'bad')")
    sql = (
        "AAYUSH SELECT a.id, b.id FROM a JOIN b ON a.id = b.user_id "
        "AND b.amount > a.minimum AND b.tag LIKE 'g%' ORDER BY b.id"
    )
    assert _rows(e, sql) == [{"a.id": 1, "b.id": 10}]
    assert _rows(e, sql) == _reference(e, sql)["rows"]


def test_hash_join_with_residual_expression_matches_nested_loop():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE l (id INT, base INT)")
    e.execute("AAYUSH CREATE TABLE r (id INT, lhs INT, factor INT)")
    e.execute("AAYUSH INSERT INTO l VALUES " + ", ".join(f"({i}, {i * 10})" for i in range(1, 41)))
    e.execute(
        "AAYUSH INSERT INTO r VALUES "
        + ", ".join(f"({i}, {(i % 40) + 1}, {i % 3})" for i in range(1, 121))
    )
    sql = (
        "AAYUSH SELECT l.id, r.id FROM l JOIN r ON l.id = r.lhs "
        "AND r.factor * 2 >= l.base / 100 ORDER BY l.id, r.id"
    )
    stmt = _stmt(sql)
    optimized = execute_select(e.db, stmt, plan=plan_select(e.db, stmt, force_join_algorithm="HASH"))
    reference = _reference(e, sql)
    assert optimized == reference
    assert any(
        node.op == "JOIN" and node.algorithm == "HASH" for node in _walk(plan_select(e.db, stmt).root)
    )


def test_non_equi_join_condition_falls_back_to_nested_loop():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE l (id INT, lo INT)")
    e.execute("AAYUSH CREATE TABLE r (id INT, hi INT)")
    e.execute("AAYUSH INSERT INTO l VALUES " + ", ".join(f"({i}, {i})" for i in range(1, 31)))
    e.execute("AAYUSH INSERT INTO r VALUES " + ", ".join(f"({i}, {i * 2})" for i in range(1, 31)))
    plan = plan_select(
        e.db, _stmt("AAYUSH SELECT l.id FROM l JOIN r ON l.id BETWEEN r.hi AND r.hi + 5")
    )
    join = next(node for node in _walk(plan.root) if node.op == "JOIN")
    assert join.algorithm == "NESTED LOOP"
    assert join.key_pairs == []


# --- statement-level behaviour -------------------------------------------


def test_select_where_update_and_delete_agree_on_the_same_predicate(employees):
    predicate = "salary BETWEEN 30000 AND 60000 AND name LIKE '%a%' AND department IS NOT NULL"
    selected = _rows(employees, f"AAYUSH SELECT id FROM employees WHERE {predicate} ORDER BY id")
    updated = employees.execute(
        f"AAYUSH UPDATE employees SET score = 1.0 WHERE {predicate}"
    )["rows_affected"]
    assert updated == len(selected)
    remaining = _rows(employees, "AAYUSH SELECT id FROM employees WHERE score = 1.0 ORDER BY id")
    assert remaining == selected
    deleted = employees.execute(f"AAYUSH DELETE FROM employees WHERE {predicate}")["rows_affected"]
    assert deleted == len(selected)
    assert _rows(employees, f"AAYUSH SELECT id FROM employees WHERE {predicate}") == []


def test_grouped_query_with_every_new_construct(employees):
    rows = _rows(
        employees,
        """
        AAYUSH SELECT
            CASE WHEN department IS NULL THEN 'unknown' ELSE department END AS dept,
            COUNT(*) AS headcount,
            SUM(CASE WHEN salary BETWEEN 50000 AND 90000 THEN salary ELSE 0 END) AS banded,
            AVG(score) * 2 AS doubled_score
        FROM employees
        WHERE name NOT LIKE '%z%'
        GROUP BY department
        HAVING COUNT(*) >= 1
        ORDER BY headcount DESC, dept
        LIMIT 3 OFFSET 1
        """,
    )
    # Ordered by headcount DESC then dept ASC: Engineering, Sales, HR, unknown.
    # OFFSET 1 drops Engineering, LIMIT 3 keeps the rest.
    assert rows == [
        {"dept": "Sales", "headcount": 2, "banded": 0, "doubled_score": 166.0},
        {"dept": "HR", "headcount": 1, "banded": 0, "doubled_score": 111.0},
        {"dept": "unknown", "headcount": 1, "banded": 0, "doubled_score": None},
    ]


def test_empty_table_with_new_expressions():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE t (id INT, v INT)")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE v IS NULL") == []
    assert _rows(e, "AAYUSH SELECT DISTINCT id FROM t ORDER BY id LIMIT 5 OFFSET 2") == []
    assert _rows(e, "AAYUSH SELECT SUM(v * 2) AS s FROM t") == [{"s": None}]
    assert _rows(e, "AAYUSH SELECT COUNT(CASE WHEN v BETWEEN 1 AND 2 THEN 1 END) AS c FROM t") == [
        {"c": 0}
    ]


def test_new_expressions_inside_a_transaction_are_isolated_until_commit(tmp_path):
    path = tmp_path / "txn.aaydb"
    e = Engine(path)
    e.execute("AAYUSH CREATE TABLE t (id INT, v INT)")
    e.execute("AAYUSH BEGIN")
    e.execute("AAYUSH INSERT INTO t VALUES (1, 10), (2, NULL)")
    e.execute("AAYUSH UPDATE t SET v = CASE WHEN v IS NULL THEN 0 ELSE v * 2 END")
    assert _rows(e, "AAYUSH SELECT id, v FROM t ORDER BY id") == [
        {"id": 1, "v": 20},
        {"id": 2, "v": 0},
    ]
    e.execute("AAYUSH ROLLBACK")
    assert _rows(e, "AAYUSH SELECT COUNT(*) AS c FROM t") == [{"c": 0}]


def test_new_expressions_persist_across_reopen(tmp_path):
    path = tmp_path / "lang.aaydb"
    e = Engine(path)
    e.execute("AAYUSH CREATE TABLE t (id INT, name TEXT, v INT)")
    e.execute("AAYUSH INSERT INTO t VALUES (1, 'Alex', 10), (2, NULL, NULL)")
    e.execute("AAYUSH UPDATE t SET v = 1 + v * 2 WHERE name IS NOT NULL")
    e.close()

    reopened = Engine(path)
    assert _rows(reopened, "AAYUSH SELECT id, v FROM t ORDER BY id") == [
        {"id": 1, "v": 21},
        {"id": 2, "v": None},
    ]
    assert _rows(reopened, "AAYUSH SELECT DISTINCT v FROM t ORDER BY v") == [{"v": 21}, {"v": None}]
    assert _rows(reopened, "AAYUSH SELECT id FROM t WHERE name LIKE 'A%'") == [{"id": 1}]
    reopened.close()


def test_format_version_and_protocol_version_are_unchanged(tmp_path):
    from aaysql import protocol
    from aaysql.storage import FORMAT_VERSION, SEGMENTED_FORMAT_VERSION

    # Version 3 remains the legacy snapshot format; new databases are v4.
    assert FORMAT_VERSION == 3
    assert SEGMENTED_FORMAT_VERSION == 4
    assert protocol.PROTOCOL_VERSION == 1

    path = tmp_path / "fmt.aaydb"
    e = Engine(path)
    e.execute("AAYUSH CREATE TABLE t (id INT, v INT)")
    e.execute("AAYUSH INSERT INTO t VALUES (1, 2)")
    e.close()
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format_version"] == 4
    assert payload["tables"]


def test_select_through_the_public_expression_entry_point(employees):
    """execute_select must accept the same statements the engine parses."""
    result = execute_select(
        employees.db,
        _stmt(
            "AAYUSH SELECT id FROM employees WHERE salary NOT BETWEEN 1 AND 2 "
            "AND department IN ('HR', 'Sales') ORDER BY id"
        ),
    )
    assert result["rows"] == [{"id": 2}, {"id": 5}]
    assert result["columns"] == ["id"]


def _walk(node):
    if node is None:
        return
    yield node
    for child in node.children:
        yield from _walk(child)
