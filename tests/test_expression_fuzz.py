"""Deterministic randomized differential tests for v0.9.0 expressions.

Each test generates small expressions from the supported node set, evaluates them
through the engine, and compares against an independent reference implementation
written here (a small three-valued interpreter plus a hand-written LIKE matcher).

Seeds are fixed, and every failure message includes the expression source and row
so a failing case can be reproduced by hand.
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.errors import ExecutionError
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import plan_select

ROWS = [
    {"id": 1, "a": 1, "b": 10, "c": 1.5, "s": "alpha", "flag": True},
    {"id": 2, "a": 2, "b": None, "c": 2.0, "s": None, "flag": False},
    {"id": 3, "a": None, "b": 30, "c": None, "s": "beta", "flag": None},
    {"id": 4, "a": 4, "b": 40, "c": 4.25, "s": "gamma", "flag": True},
    {"id": 5, "a": 0, "b": 0, "c": 0.0, "s": "", "flag": False},
    {"id": 6, "a": -3, "b": 7, "c": -1.5, "s": "alphabet", "flag": True},
    {"id": 7, "a": 5, "b": 5, "c": 5.5, "s": "delta", "flag": None},
    {"id": 8, "a": None, "b": None, "c": None, "s": None, "flag": None},
]

TEXT_PATTERNS = ["a%", "%a", "%a%", "_", "____", "a_a", "%et%", "beta", "%z%", "%", "al%a", "_e__"]
NUMERIC_COLUMNS = ["a", "b"]
FLOAT_COLUMNS = ["c"]
TEXT_COLUMNS = ["s"]


# --- independent reference semantics -------------------------------------


def like_match(value: str, pattern: str) -> bool:
    """Hand-written LIKE matcher: ``%`` any run, ``_`` exactly one character."""
    vi = pi = 0
    star = -1
    mark = 0
    while vi < len(value):
        if pi < len(pattern) and pattern[pi] in ("_",) or (
            pi < len(pattern) and pattern[pi] == value[vi]
        ):
            vi += 1
            pi += 1
        elif pi < len(pattern) and pattern[pi] == "%":
            star = pi
            mark = vi
            pi += 1
        elif star != -1:
            pi = star + 1
            mark += 1
            vi = mark
        else:
            return False
    while pi < len(pattern) and pattern[pi] == "%":
        pi += 1
    return pi == len(pattern)


def _logic(value):
    """SQL logical state of a runtime value: TRUE, FALSE, or UNKNOWN (None)."""
    if value is None:
        return None
    return bool(value)


def ref_eval(node, row):
    kind = node[0]
    if kind == "col":
        return row[node[1]]
    if kind == "lit":
        return node[1]
    if kind == "arith":
        op, left, right = node[1], ref_eval(node[2], row), ref_eval(node[3], row)
        if left is None or right is None:
            return None
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return left / right
        return left % right
    if kind == "cmp":
        op, left, right = node[1], ref_eval(node[2], row), ref_eval(node[3], row)
        if left is None or right is None:
            return None
        return {"=": left == right, "!=": left != right, "<": left < right,
                ">": left > right, "<=": left <= right, ">=": left >= right}[op]
    if kind == "isnull":
        value = ref_eval(node[2], row)
        return (value is not None) if node[1] else (value is None)
    if kind == "like":
        value = ref_eval(node[1], row)
        if value is None:
            return None
        return like_match(value, node[2])
    if kind == "notlike":
        value = ref_eval(node[1], row)
        if value is None:
            return None
        return not like_match(value, node[2])
    if kind == "in":
        value = ref_eval(node[1], row)
        if value is None:
            return None
        hit = any(value == candidate for candidate in node[2])
        unknown = any(candidate is None for candidate in node[2])
        if hit:
            return not node[3]
        return None if unknown else node[3]
    if kind == "between":
        value, low, high = ref_eval(node[1], row), ref_eval(node[2], row), ref_eval(node[3], row)
        if value is None or low is None or high is None:
            lowered = None
        else:
            lowered = value >= low and value <= high
        if node[4]:
            return None if lowered is None else not lowered
        return lowered
    if kind == "and":
        left, right = _logic(ref_eval(node[1], row)), _logic(ref_eval(node[2], row))
        if (left is False) or (right is False):
            return False
        return None if (left is None or right is None) else True
    if kind == "or":
        left, right = _logic(ref_eval(node[1], row)), _logic(ref_eval(node[2], row))
        if (left is True) or (right is True):
            return True
        return None if (left is None or right is None) else False
    if kind == "not":
        value = _logic(ref_eval(node[1], row))
        return None if value is None else not value
    if kind == "case":
        for condition, result in node[1]:
            if _logic(ref_eval(condition, row)) is True:
                return ref_eval(result, row)
        return ref_eval(node[2], row) if node[2] is not None else None
    raise AssertionError(f"unknown reference node {kind}")


def render(node) -> str:
    kind = node[0]
    if kind == "col":
        return node[1]
    if kind == "lit":
        value = node[1]
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return repr(value)
    if kind == "arith":
        return f"({render(node[2])} {node[1]} {render(node[3])})"
    if kind == "cmp":
        return f"({render(node[2])} {node[1]} {render(node[3])})"
    if kind == "isnull":
        return f"({render(node[2])} IS {'NOT ' if node[1] else ''}NULL)"
    if kind == "like":
        return f"({render(node[1])} LIKE {render(('lit', node[2]))})"
    if kind == "notlike":
        return f"({render(node[1])} NOT LIKE {render(('lit', node[2]))})"
    if kind == "in":
        items = ", ".join(render(("lit", value)) for value in node[2])
        return f"({render(node[1])} {'NOT ' if node[3] else ''}IN ({items}))"
    if kind == "between":
        return (
            f"({render(node[1])} {'NOT ' if node[4] else ''}BETWEEN "
            f"{render(node[2])} AND {render(node[3])})"
        )
    if kind == "and":
        return f"({render(node[1])} AND {render(node[2])})"
    if kind == "or":
        return f"({render(node[1])} OR {render(node[2])})"
    if kind == "not":
        return f"(NOT {render(node[1])})"
    if kind == "case":
        parts = ["CASE"]
        for condition, result in node[1]:
            parts.append(f"WHEN {render(condition)} THEN {render(result)}")
        if node[2] is not None:
            parts.append(f"ELSE {render(node[2])}")
        parts.append("END")
        return "(" + " ".join(parts) + ")"
    raise AssertionError(f"cannot render {kind}")


# --- generator -----------------------------------------------------------


def _numeric(rng, depth):
    if depth <= 0 or rng.random() < 0.45:
        if rng.random() < 0.5:
            return ("col", rng.choice(NUMERIC_COLUMNS))
        return ("lit", rng.choice([0, 1, 2, 3, 5, 10, -2, 2.5, 1.25]))
    op = rng.choice(["+", "-", "*", "/", "%"])
    left = _numeric(rng, depth - 1)
    if op in {"/", "%"}:
        # A literal non-zero divisor keeps both implementations total.
        right = ("lit", rng.choice([1, 2, 3, 4, -2]))
    else:
        right = _numeric(rng, depth - 1)
    return ("arith", op, left, right)


def _text(rng, depth):
    if rng.random() < 0.3:
        return ("lit", rng.choice(["alpha", "beta", "", "gamma", None]))
    return ("col", rng.choice(TEXT_COLUMNS))


def _boolean(rng, depth):
    if depth <= 0:
        choice = rng.random()
        if choice < 0.3:
            return ("isnull", rng.random() < 0.5, ("col", rng.choice(["a", "b", "c", "s", "flag"])))
        if choice < 0.45:
            return ("lit", rng.choice([True, False, None]))
        if choice < 0.7:
            return ("cmp", rng.choice(["=", "!=", "<", ">", "<=", ">="]), _numeric(rng, 1), _numeric(rng, 1))
        value = _text(rng, 1)
        pattern = rng.choice(TEXT_PATTERNS)
        return ("like" if rng.random() < 0.6 else "notlike", value, pattern)
    pick = rng.random()
    if pick < 0.18:
        return ("and", _boolean(rng, depth - 1), _boolean(rng, depth - 1))
    if pick < 0.36:
        return ("or", _boolean(rng, depth - 1), _boolean(rng, depth - 1))
    if pick < 0.48:
        return ("not", _boolean(rng, depth - 1))
    if pick < 0.62:
        return ("cmp", rng.choice(["=", "!=", "<", ">", "<=", ">="]), _numeric(rng, 2), _numeric(rng, 2))
    if pick < 0.72:
        return ("isnull", rng.random() < 0.5, _numeric(rng, 2))
    if pick < 0.8:
        value = _text(rng, 2)
        pattern = rng.choice(TEXT_PATTERNS)
        return ("like" if rng.random() < 0.6 else "notlike", value, pattern)
    if pick < 0.88:
        column = rng.choice(NUMERIC_COLUMNS)
        items = [rng.choice([0, 1, 2, 4, 5, 10, None]) for _ in range(rng.randint(1, 4))]
        return ("in", ("col", column), items, rng.random() < 0.5)
    if pick < 0.94:
        column = rng.choice(NUMERIC_COLUMNS)
        low, high = sorted([rng.choice([-3, 0, 1, 2, 5, 10]), rng.choice([-3, 0, 1, 2, 5, 10])])
        return ("between", ("col", column), ("lit", low), ("lit", high), rng.random() < 0.4)
    # In a boolean position the CASE branches are boolean too, keeping the
    # generated expression type-correct for both the engine and the reference.
    branches = []
    for _ in range(rng.randint(1, 2)):
        branches.append((_boolean(rng, depth - 1), _boolean(rng, max(depth - 2, 0))))
    else_expr = _boolean(rng, max(depth - 2, 0)) if rng.random() < 0.7 else None
    return ("case", branches, else_expr)


# --- fixtures ------------------------------------------------------------


@pytest.fixture()
def engine():
    e = Engine()
    e.execute(
        "AAYUSH CREATE TABLE t (id INT, a INT, b INT, c FLOAT, s TEXT, flag BOOL)"
    )
    values = ", ".join(
        "({}, {}, {}, {}, {}, {})".format(
            row["id"],
            "NULL" if row["a"] is None else row["a"],
            "NULL" if row["b"] is None else row["b"],
            "NULL" if row["c"] is None else row["c"],
            "NULL" if row["s"] is None else "'" + row["s"] + "'",
            "NULL" if row["flag"] is None else ("TRUE" if row["flag"] else "FALSE"),
        )
        for row in ROWS
    )
    e.execute(f"AAYUSH INSERT INTO t VALUES {values}")
    return e


def _stmt(sql):
    return Parser(Lexer().tokenize(sql)).parse()


@pytest.mark.parametrize("seed", range(12))
def test_random_boolean_expressions_match_reference(engine, seed):
    rng = random.Random(seed)
    for index in range(12):
        node = _boolean(rng, 3)
        sql_expr = render(node)
        result = engine.execute(f"AAYUSH SELECT id, {sql_expr} AS r FROM t ORDER BY id")
        got = {row["id"]: row["r"] for row in result["rows"]}
        expected = {row["id"]: _logic(ref_eval(node, row)) for row in ROWS}
        assert got == expected, f"seed={seed} case={index} expr={sql_expr}"

        # The same predicate must select exactly the TRUE rows in WHERE.
        selected = engine.execute(
            f"AAYUSH SELECT id FROM t WHERE {sql_expr} ORDER BY id"
        )["rows"]
        assert [row["id"] for row in selected] == sorted(
            i for i, value in expected.items() if value is True
        ), f"WHERE mismatch: seed={seed} expr={sql_expr}"


@pytest.mark.parametrize("seed", range(8))
def test_random_numeric_projections_match_reference(engine, seed):
    rng = random.Random(1000 + seed)
    for index in range(10):
        node = _numeric(rng, 3)
        sql_expr = render(node)
        result = engine.execute(f"AAYUSH SELECT id, {sql_expr} AS r FROM t ORDER BY id")
        got = {row["id"]: row["r"] for row in result["rows"]}
        expected = {row["id"]: ref_eval(node, row) for row in ROWS}
        assert got == expected, f"seed={seed} case={index} expr={sql_expr}"


@pytest.mark.parametrize("seed", range(8))
def test_random_update_and_delete_match_select_predicates(engine, seed):
    rng = random.Random(5000 + seed)
    for index in range(5):
        node = _boolean(rng, 2)
        sql_expr = render(node)
        matched = {
            row["id"] for row in ROWS if _logic(ref_eval(node, row)) is True
        }
        engine.execute("AAYUSH UPDATE t SET a = a WHERE " + sql_expr)
        updated = engine.execute(
            f"AAYUSH SELECT id FROM t WHERE {sql_expr} ORDER BY id"
        )["rows"]
        assert {row["id"] for row in updated} == matched, f"UPDATE: seed={seed} expr={sql_expr}"

        engine.execute("AAYUSH DELETE FROM t WHERE " + sql_expr)
        remaining = {row["id"] for row in engine.execute("AAYUSH SELECT id FROM t")["rows"]}
        assert remaining == {row["id"] for row in ROWS} - matched, (
            f"DELETE: seed={seed} expr={sql_expr}"
        )
        # Rebuild the table for the next case.
        engine.db.table("t").rows.clear()
        engine.db.table("t").touch()
        rows = ", ".join(
            "({}, {}, {}, {}, {}, {})".format(
                r["id"],
                "NULL" if r["a"] is None else r["a"],
                "NULL" if r["b"] is None else r["b"],
                "NULL" if r["c"] is None else r["c"],
                "NULL" if r["s"] is None else "'" + r["s"] + "'",
                "NULL" if r["flag"] is None else ("TRUE" if r["flag"] else "FALSE"),
            )
            for r in ROWS
        )
        engine.execute(f"AAYUSH INSERT INTO t VALUES {rows}")


@pytest.mark.parametrize("seed", range(6))
def test_random_index_and_table_scan_results_are_identical(engine, seed):
    engine.execute("AAYUSH CREATE INDEX idx_a ON t (a)")
    engine.execute("AAYUSH CREATE INDEX idx_b ON t (b)")
    rng = random.Random(9000 + seed)
    for index in range(6):
        node = _boolean(rng, 2)
        sql_expr = render(node)
        sql = (
            f"AAYUSH SELECT id, a FROM t WHERE {sql_expr} "
            f"ORDER BY id LIMIT 5 OFFSET {index % 3}"
        )
        stmt = _stmt(sql)
        by_scan = execute_select(engine.db, stmt, plan=plan_select(engine.db, stmt, force_access_path="TABLE"))
        by_index = execute_select(engine.db, stmt, plan=plan_select(engine.db, stmt, force_access_path="INDEX"))
        assert by_scan == by_index, f"index mismatch: seed={seed} expr={sql_expr}"


def test_random_case_expressions_are_deterministic(engine):
    for _ in range(10):
        node = ("case", [(("cmp", ">", ("col", "a"), ("lit", 2)), ("lit", "big"))],
                ("lit", "small"))
        sql_expr = render(node)
        first = engine.execute(f"AAYUSH SELECT id, {sql_expr} AS r FROM t ORDER BY id")["rows"]
        second = engine.execute(f"AAYUSH SELECT id, {sql_expr} AS r FROM t ORDER BY id")["rows"]
        assert first == second
        assert first == [
            {"id": row["id"], "r": ref_eval(node, row)} for row in ROWS
        ]


def test_random_arithmetic_errors_do_not_depend_on_row_order():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE t (id INT, v INT)")
    e.execute("AAYUSH INSERT INTO t VALUES (1, 5), (2, 0), (3, 7)")
    with pytest.raises(ExecutionError, match="Division by zero"):
        e.execute("AAYUSH SELECT id, 100 / v AS r FROM t")
    # Nothing was persisted or mutated by a failed SELECT.
    assert e.execute("AAYUSH SELECT id, v FROM t ORDER BY id")["rows"] == [
        {"id": 1, "v": 5},
        {"id": 2, "v": 0},
        {"id": 3, "v": 7},
    ]


def test_like_reference_matches_engine_for_all_generated_patterns(engine):
    for pattern in TEXT_PATTERNS:
        for row in ROWS:
            value = row["s"]
            result = engine.execute(
                f"AAYUSH SELECT id, s FROM t WHERE s LIKE '{pattern}' ORDER BY id"
            )["rows"]
            expected = [
                r["id"]
                for r in ROWS
                if r["s"] is not None and like_match(r["s"], pattern)
            ]
            assert [r["id"] for r in result] == expected, (pattern, value)
