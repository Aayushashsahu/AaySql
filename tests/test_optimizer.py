"""Correctness tests for the v0.7.0 cost-based planner and hash joins.

Every optimization is checked against the reference path (nested-loop joins and
table scans, both forceable through ``plan_select``) on the exact same data.
Row order is part of the contract: comparisons are made against the full ordered
row list, never against sorted or set-normalized output.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import PlanNode, format_plan, plan_select

# --- helpers --------------------------------------------------------------


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _reference(engine: Engine, sql: str):
    """Run a query through the v0.6 style path: table scans and nested loops."""
    stmt = _stmt(sql)
    plan = plan_select(engine.db, stmt, force_join_algorithm="NESTED LOOP", force_access_path="TABLE")
    return execute_select(engine.db, stmt, plan=plan)


def _optimized(engine: Engine, sql: str):
    return engine.execute(sql)


def _plan(engine: Engine, sql: str):
    return plan_select(engine.db, _stmt(sql))


def _walk(node: PlanNode):
    yield node
    for child in node.children:
        yield from _walk(child)


def _find_op(root: PlanNode, op: str) -> PlanNode | None:
    return next((node for node in _walk(root) if node.op == op), None)


def _find_scan(root: PlanNode, ref: str) -> PlanNode:
    for node in _walk(root):
        if node.op in ("TABLE SCAN", "INDEX SCAN") and node.ref == ref:
            return node
    raise AssertionError(f"no scan for source '{ref}'")


def _seed(engine: Engine, *, users: int = 24, orders: int = 30, null_keys: bool = True) -> None:
    engine.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
    engine.execute("AAYUSH CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total INT, note TEXT)")
    for i in range(1, users + 1):
        engine.execute(f"AAYUSH INSERT INTO users VALUES ({i}, 'u{i}', {18 + i % 4})")
    values = []
    for j in range(1, orders + 1):
        user_id = "NULL" if (null_keys and j % 11 == 0) else str(j % 9)
        note = "NULL" if j % 7 == 0 else f"'n{j}'"
        values.append(f"({j}, {user_id}, {j % 5}, {note})")
    engine.execute("AAYUSH INSERT INTO orders VALUES " + ", ".join(values))


@pytest.fixture()
def engine() -> Engine:
    engine = Engine()
    yield engine
    engine.close()


def _assert_same(engine: Engine, sql: str):
    reference = _reference(engine, sql)
    optimized = _optimized(engine, sql)
    assert optimized["columns"] == reference["columns"]
    assert optimized["rows"] == reference["rows"]
    assert optimized["row_count"] == reference["row_count"]
    return optimized


# --- hash join equivalence ------------------------------------------------


JOIN_QUERIES = [
    "AAYUSH SELECT u.id, o.id AS oid FROM users u INNER JOIN orders o ON u.id = o.user_id",
    "AAYUSH SELECT u.id, o.id AS oid FROM users u LEFT JOIN orders o ON u.id = o.user_id",
    "AAYUSH SELECT u.name, o.total FROM users u INNER JOIN orders o ON u.id = o.user_id WHERE u.age > 18",
    "AAYUSH SELECT u.id, o.id AS oid FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE o.total > 2",
    "AAYUSH SELECT u.id, o.id AS oid FROM users u INNER JOIN orders o ON u.id = o.user_id AND o.total > 1",
    "AAYUSH SELECT u.id, o.id AS oid FROM users u LEFT JOIN orders o ON u.id = o.user_id AND o.note = 'n7'",
    "AAYUSH SELECT u.id, COUNT(*) AS n FROM users u INNER JOIN orders o ON u.id = o.user_id GROUP BY u.id",
    "AAYUSH SELECT u.id, SUM(o.total) AS s FROM users u LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.id HAVING SUM(o.total) > 2",
    "AAYUSH SELECT u.id, o.id AS oid FROM users u INNER JOIN orders o ON u.id = o.user_id ORDER BY o.total DESC, u.id DESC LIMIT 7",
    "AAYUSH SELECT * FROM users u LEFT JOIN orders o ON u.id = o.user_id",
]


@pytest.mark.parametrize("sql", JOIN_QUERIES)
def test_optimized_join_matches_reference_exactly(engine, sql):
    _seed(engine)
    _assert_same(engine, sql)


@pytest.mark.parametrize("sql", JOIN_QUERIES)
def test_optimized_join_matches_reference_with_indexes(engine, sql):
    _seed(engine)
    engine.execute("AAYUSH CREATE INDEX idx_orders_user ON orders (user_id)")
    engine.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    _assert_same(engine, sql)


def test_hash_join_is_selected_for_large_equality_join(engine):
    _seed(engine)
    plan = _plan(engine, JOIN_QUERIES[0])
    join = _find_op(plan.root, "JOIN")
    assert join is not None
    assert join.algorithm == "HASH"
    assert join.key_pairs == [("u", "id", "o", "user_id")]
    assert "JOIN: HASH (INNER)" in format_plan(plan)
    assert "HASH KEYS: u.id=o.user_id" in format_plan(plan)


def test_small_join_keeps_nested_loop_reference(engine):
    engine.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
    engine.execute("AAYUSH CREATE TABLE orders (id INT PRIMARY KEY, user_id INT)")
    engine.execute("AAYUSH INSERT INTO users VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    engine.execute("AAYUSH INSERT INTO orders VALUES (10, 1), (11, 3), (12, 9)")
    sql = "AAYUSH SELECT u.id, o.id AS oid FROM users u INNER JOIN orders o ON u.id = o.user_id"
    plan = _plan(engine, sql)
    assert _find_op(plan.root, "JOIN").algorithm == "NESTED LOOP"
    _assert_same(engine, sql)


def test_duplicate_keys_produce_all_combinations_in_reference_order(engine):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT)")
    for i in range(1, 21):
        engine.execute(f"AAYUSH INSERT INTO leftt VALUES ({i}, {i % 3})")
    for i in range(1, 21):
        engine.execute(f"AAYUSH INSERT INTO rightt VALUES ({i}, {i % 3})")
    sql = "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l INNER JOIN rightt r ON l.k = r.k"
    result = _assert_same(engine, sql)
    # 3 keys, each appearing 7/7/6 times on both sides: every pair must be present.
    assert len(result["rows"]) == 7 * 7 + 7 * 7 + 6 * 6
    assert _find_op(_plan(engine, sql).root, "JOIN").algorithm == "HASH"
    # Reference order is left row, then right row per left row.
    first_left = result["rows"][0]["lid"]
    assert [row["lid"] for row in result["rows"][:7]] == [first_left] * 7
    assert [row["rid"] for row in result["rows"][:7]] == sorted(
        row["rid"] for row in result["rows"][:7]
    )


def test_null_keys_never_match_in_hash_join(engine):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT)")
    rows_left = [(i, "NULL" if i % 4 == 0 else str(i % 5)) for i in range(1, 21)]
    rows_right = [(i, "NULL" if i % 5 == 0 else str(i % 5)) for i in range(1, 21)]
    engine.execute(
        "AAYUSH INSERT INTO leftt VALUES " + ", ".join(f"({i}, {k})" for i, k in rows_left)
    )
    engine.execute(
        "AAYUSH INSERT INTO rightt VALUES " + ", ".join(f"({i}, {k})" for i, k in rows_right)
    )
    sql = "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l INNER JOIN rightt r ON l.k = r.k"
    result = _assert_same(engine, sql)
    matched_left = {row["lid"] for row in result["rows"]}
    assert all(left_id % 4 != 0 for left_id in matched_left)
    matched_right = {row["rid"] for row in result["rows"]}
    assert all(right_id % 5 != 0 for right_id in matched_right)

    left_sql = "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l LEFT JOIN rightt r ON l.k = r.k"
    left_result = _assert_same(engine, left_sql)
    null_extended = [row for row in left_result["rows"] if row["rid"] is None]
    # A left row is null-extended exactly when it matched nothing: either its own
    # key is NULL or no right row carries that key (right-side NULL keys never match).
    assert {row["lid"] for row in null_extended} == {i for i in range(1, 21)} - matched_left


def test_all_null_keys_produce_no_matches(engine):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT)")
    engine.execute(
        "AAYUSH INSERT INTO leftt VALUES " + ", ".join(f"({i}, NULL)" for i in range(1, 25))
    )
    engine.execute(
        "AAYUSH INSERT INTO rightt VALUES " + ", ".join(f"({i}, NULL)" for i in range(1, 25))
    )
    sql = "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l INNER JOIN rightt r ON l.k = r.k"
    result = _assert_same(engine, sql)
    assert result["rows"] == []


def test_composite_join_keys(engine):
    engine.execute("AAYUSH CREATE TABLE pairs (a INT, b INT, v INT)")
    engine.execute("AAYUSH CREATE TABLE tags (a INT, b INT, label TEXT)")
    engine.execute(
        "AAYUSH INSERT INTO pairs VALUES " + ", ".join(
            f"({i}, {i % 4}, {i})" for i in range(1, 26)
        )
    )
    engine.execute(
        "AAYUSH INSERT INTO tags VALUES " + ", ".join(
            f"({i}, {(i + 1) % 4}, 't{i}')" for i in range(1, 26)
        )
    )
    sql = "AAYUSH SELECT p.a AS pa, t.a AS ta FROM pairs p INNER JOIN tags t ON p.a = t.a AND p.b = t.b"
    result = _assert_same(engine, sql)
    assert result["rows"] == []
    join = _find_op(_plan(engine, sql).root, "JOIN")
    assert join.algorithm == "HASH"
    assert join.key_pairs == [("p", "a", "t", "a"), ("p", "b", "t", "b")]

    sql2 = "AAYUSH SELECT p.a AS pa, t.a AS ta FROM pairs p INNER JOIN tags t ON p.b = t.b AND p.a = t.a"
    _assert_same(engine, sql2)


def test_residual_predicate_is_fully_evaluated_on_hash_candidates(engine):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT, total INT)")
    engine.execute(
        "AAYUSH INSERT INTO leftt VALUES " + ", ".join(f"({i}, {i % 4})" for i in range(1, 25))
    )
    engine.execute(
        "AAYUSH INSERT INTO rightt VALUES " + ", ".join(
            f"({i}, {i % 4}, {i % 3})" for i in range(1, 25)
        )
    )
    sql = (
        "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l "
        "INNER JOIN rightt r ON l.k = r.k AND r.total > 0 AND r.id > 4"
    )
    result = _assert_same(engine, sql)
    assert result["rows"]
    assert all(row["rid"] > 4 for row in result["rows"])
    assert _find_op(_plan(engine, sql).root, "JOIN").algorithm == "HASH"


def test_non_equi_join_falls_back_to_nested_loop(engine):
    _seed(engine)
    sql = "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u INNER JOIN orders o ON u.id < o.user_id"
    plan = _plan(engine, sql)
    join = _find_op(plan.root, "JOIN")
    assert join.algorithm == "NESTED LOOP"
    assert "no hashable equality key" in join.why
    _assert_same(engine, sql)


def test_or_condition_join_falls_back_to_nested_loop(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id OR u.id = 1"
    )
    join = _find_op(_plan(engine, sql).root, "JOIN")
    assert join.algorithm == "NESTED LOOP"
    _assert_same(engine, sql)


def test_three_table_chain_matches_reference(engine):
    _seed(engine)
    engine.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, order_id INT, sku TEXT)")
    engine.execute(
        "AAYUSH INSERT INTO items VALUES " + ", ".join(
            f"({i}, {i % 12 + 1}, 'sku{i % 4}')" for i in range(1, 26)
        )
    )
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid, i.id AS iid FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id "
        "LEFT JOIN items i ON o.id = i.order_id"
    )
    _assert_same(engine, sql)


@pytest.mark.parametrize(
    "left_rows,right_rows",
    [(0, 0), (0, 20), (20, 0), (1, 1), (1, 40), (40, 1), (25, 25)],
)
def test_adversarial_join_sizes_match_reference(engine, left_rows, right_rows):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT)")
    if left_rows:
        engine.execute(
            "AAYUSH INSERT INTO leftt VALUES "
            + ", ".join(f"({i}, {i % 3})" for i in range(1, left_rows + 1))
        )
    if right_rows:
        engine.execute(
            "AAYUSH INSERT INTO rightt VALUES "
            + ", ".join(f"({i}, {i % 3})" for i in range(1, right_rows + 1))
        )
    for sql in (
        "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l INNER JOIN rightt r ON l.k = r.k",
        "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l LEFT JOIN rightt r ON l.k = r.k",
    ):
        _assert_same(engine, sql)


def test_skewed_key_distribution_matches_reference(engine):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT)")
    engine.execute(
        "AAYUSH INSERT INTO leftt VALUES "
        + ", ".join(f"({i}, {1 if i % 10 else i})" for i in range(1, 41))
    )
    engine.execute(
        "AAYUSH INSERT INTO rightt VALUES "
        + ", ".join(f"({i}, {1 if i % 8 else i})" for i in range(1, 41))
    )
    sql = "AAYUSH SELECT l.id AS lid, r.id AS rid FROM leftt l INNER JOIN rightt r ON l.k = r.k"
    _assert_same(engine, sql)


# --- predicate pushdown ---------------------------------------------------


def test_single_table_predicate_is_pushed_into_scan(engine):
    _seed(engine)
    plan = _plan(engine, "AAYUSH SELECT * FROM users WHERE age > 18")
    scan = _find_scan(plan.root, "users")
    assert [format_expr_text(p) for p in scan.predicates] == ["age > 18"]
    assert _find_op(plan.root, "FILTER") is None


def format_expr_text(predicate):
    from aaysql.planner import format_expr

    return format_expr(predicate)


def test_and_conjuncts_are_pushed_but_or_is_never_split(engine):
    _seed(engine)
    plan = _plan(engine, "AAYUSH SELECT * FROM users WHERE age > 18 OR id = 1")
    scan = _find_scan(plan.root, "users")
    assert len(scan.predicates) == 1
    assert "OR" in format_expr_text(scan.predicates[0])

    plan2 = _plan(engine, "AAYUSH SELECT * FROM users WHERE age > 18 AND id > 2")
    scan2 = _find_scan(plan2.root, "users")
    assert [format_expr_text(p) for p in scan2.predicates] == ["age > 18", "id > 2"]


def test_left_join_keeps_right_side_where_predicate_after_the_join(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "LEFT JOIN orders o ON u.id = o.user_id WHERE o.total > 2"
    )
    plan = _plan(engine, sql)
    assert _find_scan(plan.root, "o").predicates == []
    post = _find_op(plan.root, "FILTER")
    assert post is not None
    assert [format_expr_text(p) for p in post.predicates] == ["o.total > 2"]
    _assert_same(engine, sql)


def test_left_join_pushes_right_only_on_predicate_into_right_input(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "LEFT JOIN orders o ON u.id = o.user_id AND o.total > 2"
    )
    plan = _plan(engine, sql)
    assert [format_expr_text(p) for p in _find_scan(plan.root, "o").predicates] == ["o.total > 2"]
    assert _find_scan(plan.root, "u").predicates == []
    _assert_same(engine, sql)


def test_left_join_never_pushes_left_only_on_predicate_below_the_join(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "LEFT JOIN orders o ON u.id = o.user_id AND u.age > 18"
    )
    plan = _plan(engine, sql)
    assert _find_scan(plan.root, "u").predicates == []
    _assert_same(engine, sql)


def test_inner_join_pushes_single_side_on_predicates(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id AND u.age > 18 AND o.total > 2"
    )
    plan = _plan(engine, sql)
    assert [format_expr_text(p) for p in _find_scan(plan.root, "u").predicates] == ["u.age > 18"]
    assert [format_expr_text(p) for p in _find_scan(plan.root, "o").predicates] == ["o.total > 2"]
    _assert_same(engine, sql)


def test_left_join_pushes_preserved_side_where_predicate(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "LEFT JOIN orders o ON u.id = o.user_id WHERE u.age > 19"
    )
    plan = _plan(engine, sql)
    assert [format_expr_text(p) for p in _find_scan(plan.root, "u").predicates] == ["u.age > 19"]
    _assert_same(engine, sql)


def test_join_with_where_on_both_sides_stays_post_join(engine):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id WHERE u.age > o.total"
    )
    plan = _plan(engine, sql)
    post = _find_op(plan.root, "FILTER")
    assert post is not None
    assert [format_expr_text(p) for p in post.predicates] == ["u.age > o.total"]
    _assert_same(engine, sql)


# --- statistics -----------------------------------------------------------


def test_statistics_are_exact_for_small_tables(engine):
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, k INT, v TEXT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 5, 'a'), (2, 5, 'b'), (3, NULL, 'a'), (4, 9, NULL)")
    stats = engine.db.stats.get(engine.db.table("t"))
    assert stats.row_count == 4
    assert stats.column("k").null_count == 1
    assert stats.column("k").distinct == 2
    assert stats.column("k").distinct_is_exact
    assert (stats.column("k").minimum, stats.column("k").maximum) == (5, 9)
    assert stats.column("v").null_count == 1
    assert "exact" in stats.method


def test_statistics_are_independent_of_indexes(engine):
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES " + ", ".join(f"({i}, {i % 4})" for i in range(1, 41)))
    before = engine.db.stats.get(engine.db.table("t"))
    engine.execute("AAYUSH CREATE INDEX idx_t_k ON t (k)")
    after = engine.db.stats.get(engine.db.table("t"))
    assert before == after


def test_statistics_invalidate_on_insert_update_delete(engine):
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH INSERT INTO t VALUES (1, 1), (2, 2), (3, 2)")
    assert engine.db.stats.get(engine.db.table("t")).row_count == 3

    engine.execute("AAYUSH INSERT INTO t VALUES (4, 4)")
    assert engine.db.stats.get(engine.db.table("t")).row_count == 4

    engine.execute("AAYUSH UPDATE t SET k = 7 WHERE id = 1")
    stats = engine.db.stats.get(engine.db.table("t"))
    assert stats.column("k").maximum == 7
    assert stats.column("k").distinct == 3  # {7, 2, 4}

    engine.execute("AAYUSH DELETE FROM t WHERE id = 2")
    assert engine.db.stats.get(engine.db.table("t")).row_count == 3


def test_statistics_survive_rollback_and_reopen(tmp_path: Path):
    db_file = tmp_path / "stats.aaydb"
    with Engine(db_file) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, k INT)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1, 1), (2, 2)")
        assert engine.db.stats.get(engine.db.table("t")).row_count == 2

        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO t VALUES (3, 3), (4, 4)")
        assert engine.db.stats.get(engine.db.table("t")).row_count == 4
        engine.execute("AAYUSH ROLLBACK")
        assert engine.db.stats.get(engine.db.table("t")).row_count == 2

    with Engine(db_file) as reopened:
        stats = reopened.db.stats.get(reopened.db.table("t"))
        assert stats.row_count == 2
        assert stats.column("k").distinct == 2


def test_distinct_estimate_is_bounded_and_labelled(engine):
    from aaysql.statistics import MAX_EXACT_DISTINCT

    total = MAX_EXACT_DISTINCT + 1200
    engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, k INT)")
    chunk = []
    for i in range(1, total + 1):
        chunk.append(f"({i}, {i})")
        if len(chunk) == 500:
            engine.execute("AAYUSH INSERT INTO t VALUES " + ", ".join(chunk))
            chunk = []
    if chunk:
        engine.execute("AAYUSH INSERT INTO t VALUES " + ", ".join(chunk))

    stats = engine.db.stats.get(engine.db.table("t"))
    column = stats.column("k")
    assert stats.row_count == total
    assert column.distinct_is_exact is False
    assert column.null_count == 0
    # A bounded estimate must stay in the right order of magnitude.
    assert total * 0.5 <= column.distinct <= total * 1.5


# --- cost-based access paths ---------------------------------------------


def test_equality_predicate_uses_index_and_matches_table_scan(engine):
    _seed(engine, users=40, orders=40)
    engine.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    sql = "AAYUSH SELECT id FROM users WHERE age = 19"
    scan = _find_scan(_plan(engine, sql).root, "users")
    assert scan.op == "INDEX SCAN"
    assert scan.index_name == "idx_users_age"
    _assert_same(engine, sql)


def test_narrow_range_uses_index_while_wide_range_prefers_table_scan(engine):
    engine.execute("AAYUSH CREATE TABLE big (id INT PRIMARY KEY, k INT)")
    rows = ", ".join(f"({i}, {i})" for i in range(1, 3001))
    engine.execute(f"AAYUSH INSERT INTO big VALUES {rows}")
    engine.execute("AAYUSH CREATE INDEX idx_big_k ON big (k)")

    assert _find_scan(_plan(engine, "AAYUSH SELECT id FROM big WHERE k >= 2990").root, "big").op == "INDEX SCAN"

    wide = _find_scan(_plan(engine, "AAYUSH SELECT id FROM big WHERE k >= 0").root, "big")
    assert wide.op == "TABLE SCAN"
    assert "table scan estimated cheaper" in wide.why

    for sql in (
        "AAYUSH SELECT id FROM big WHERE k >= 2990",
        "AAYUSH SELECT id FROM big WHERE k >= 0",
        "AAYUSH SELECT COUNT(*) AS n FROM big WHERE k > 1500",
    ):
        _assert_same(engine, sql)


def test_disabling_an_access_path_never_changes_results(engine):
    _seed(engine)
    engine.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    engine.execute("AAYUSH CREATE INDEX idx_orders_user ON orders (user_id)")
    for sql in JOIN_QUERIES:
        indexed = _optimized(engine, sql)
        table_only = _reference(engine, sql)
        assert indexed["rows"] == table_only["rows"]


def test_force_flags_select_the_reference_operators(engine):
    _seed(engine)
    stmt = _stmt(JOIN_QUERIES[0])
    plan = plan_select(engine.db, stmt, force_join_algorithm="NESTED LOOP", force_access_path="TABLE")
    assert _find_op(plan.root, "JOIN").algorithm == "NESTED LOOP"
    assert all(node.op == "TABLE SCAN" for node in _walk(plan.root) if node.op in ("TABLE SCAN", "INDEX SCAN"))


def test_estimated_rows_are_labelled_as_estimates(engine):
    _seed(engine)
    plan = _plan(engine, JOIN_QUERIES[0])
    text = format_plan(plan)
    assert "ESTIMATED ROWS: ~" in text
    assert "not measurements" in text
    assert plan.root.estimated_rows >= 0


# --- EXPLAIN --------------------------------------------------------------


def test_plain_explain_does_not_execute_the_select(engine, monkeypatch):
    from aaysql import execution

    _seed(engine)

    def explode(*args, **kwargs):
        raise AssertionError("plain EXPLAIN must not execute the SELECT")

    monkeypatch.setattr(execution, "execute_select", explode)
    message = engine.execute("AAYUSH EXPLAIN SELECT * FROM users WHERE age > 18")["message"]
    assert "TABLE SCAN: users" in message
    assert "ROWS SCANNED" not in message
    assert "OPERATORS:" not in message


def test_explain_does_not_change_the_database_file(tmp_path: Path):
    db_file = tmp_path / "explain.aaydb"
    with Engine(db_file) as engine:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, k INT)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1, 1), (2, 2)")
        before = db_file.read_bytes()
        engine.execute("AAYUSH EXPLAIN SELECT * FROM t WHERE k = 1")
        engine.execute("AAYUSH EXPLAIN ANALYZE SELECT * FROM t WHERE k = 1")
        assert db_file.read_bytes() == before


def test_explain_analyze_reports_real_operator_metrics(engine):
    _seed(engine)
    message = engine.execute(
        "AAYUSH EXPLAIN ANALYZE SELECT u.id, COUNT(*) AS n FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id GROUP BY u.id ORDER BY n DESC LIMIT 3"
    )["message"]
    assert "HASH JOIN (INNER)" in message
    assert "build_rows=" in message
    assert "probe_rows=" in message
    assert "bucket_matches=" in message
    assert "residual_rejected=" in message
    assert "AGGREGATE (GROUP BY u.id):" in message
    assert "SORT:" in message
    assert "LIMIT:" in message
    assert "ROWS RETURNED: 3" in message
    assert "EXECUTION TIME:" in message


def test_explain_analyze_reports_nested_loop_comparisons(engine):
    engine.execute("AAYUSH CREATE TABLE leftt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH CREATE TABLE rightt (id INT PRIMARY KEY, k INT)")
    engine.execute("AAYUSH INSERT INTO leftt VALUES (1, 1), (2, 2), (3, 3)")
    engine.execute("AAYUSH INSERT INTO rightt VALUES (1, 1), (2, 2), (3, 9)")
    message = engine.execute(
        "AAYUSH EXPLAIN ANALYZE SELECT l.id, r.id AS rid FROM leftt l "
        "INNER JOIN rightt r ON l.k = r.k"
    )["message"]
    assert "NESTED LOOP JOIN (INNER)" in message
    assert "comparisons=9" in message


def test_explain_analyze_rows_scanned_matches_reference_metrics(engine):
    _seed(engine)
    engine.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    sql = "AAYUSH SELECT id FROM users WHERE age = 19"
    analyzed = engine.execute("AAYUSH EXPLAIN ANALYZE " + sql[len("AAYUSH "):])["message"]
    assert "INDEX USED: idx_users_age" in analyzed
    result = engine.execute(sql)
    assert f"ROWS RETURNED: {result['row_count']}" in analyzed
    assert f"ROWS SCANNED: {len(result['rows'])}" in analyzed


# --- regression: pushdown must never change SQL semantics -----------------


@pytest.mark.parametrize(
    "where",
    [
        "o.total IS NULL",
        "NOT (o.total > 2)",
        "o.total > 2 OR u.age > 18",
        "o.total = NULL",
    ],
)
def test_unsupported_or_tricky_predicates_raise_or_match_reference(engine, where):
    _seed(engine)
    sql = (
        "AAYUSH SELECT u.id AS uid, o.id AS oid FROM users u "
        f"LEFT JOIN orders o ON u.id = o.user_id WHERE {where}"
    )
    try:
        expected = _reference(engine, sql)
    except Exception as error:  # noqa: BLE001 - the reference path defines the contract
        with pytest.raises(type(error)):
            _optimized(engine, sql)
    else:
        actual = _optimized(engine, sql)
        assert actual["rows"] == expected["rows"]
