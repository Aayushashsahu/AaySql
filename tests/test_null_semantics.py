import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql import Engine
from aaysql.errors import ExecutionError
from aaysql.query import (
    compare_values,
    is_sql_true,
    sql_and,
    sql_not,
    sql_or,
)

UNKNOWN = None
TRUE = True
FALSE = False


def _seed_employees() -> Engine:
    e = Engine()
    e.execute(
        """
        AAYUSH CREATE TABLE employees (
            id INT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT,
            salary INT
        )
        """
    )
    e.execute(
        """
        AAYUSH INSERT INTO employees VALUES
            (1, 'Asha', 'Engineering', 80000),
            (2, 'Kabir', NULL, 40000),
            (3, 'Tara', NULL, NULL)
        """
    )
    return e


# --- THREE-VALUED TRUTH TABLES ---


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (TRUE, TRUE, TRUE),
        (TRUE, FALSE, FALSE),
        (TRUE, UNKNOWN, UNKNOWN),
        (FALSE, TRUE, FALSE),
        (FALSE, FALSE, FALSE),
        (FALSE, UNKNOWN, FALSE),
        (UNKNOWN, TRUE, UNKNOWN),
        (UNKNOWN, FALSE, FALSE),
        (UNKNOWN, UNKNOWN, UNKNOWN),
    ],
)
def test_sql_and_truth_table(left, right, expected):
    assert sql_and(left, right) is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (TRUE, TRUE, TRUE),
        (TRUE, FALSE, TRUE),
        (TRUE, UNKNOWN, TRUE),
        (FALSE, TRUE, TRUE),
        (FALSE, FALSE, FALSE),
        (FALSE, UNKNOWN, UNKNOWN),
        (UNKNOWN, TRUE, TRUE),
        (UNKNOWN, FALSE, UNKNOWN),
        (UNKNOWN, UNKNOWN, UNKNOWN),
    ],
)
def test_sql_or_truth_table(left, right, expected):
    assert sql_or(left, right) is expected


@pytest.mark.parametrize(("value", "expected"), [(TRUE, FALSE), (FALSE, TRUE), (UNKNOWN, UNKNOWN)])
def test_sql_not_truth_table(value, expected):
    assert sql_not(value) is expected


@pytest.mark.parametrize("op", ["=", "!=", "<>", "<", "<=", ">", ">="])
def test_comparison_with_null_is_unknown(op: str):
    assert compare_values(op, None, 1) is None
    assert compare_values(op, 1, None) is None
    assert compare_values(op, None, None) is None


def test_predicate_accepts_only_true():
    assert is_sql_true(True) is True
    assert is_sql_true(False) is False
    assert is_sql_true(None) is False


# --- WHERE ---


def test_where_comparison_against_null_matches_no_rows():
    e = _seed_employees()
    assert e.execute("AAYUSH SELECT id FROM employees WHERE salary = NULL")["rows"] == []
    assert e.execute("AAYUSH SELECT id FROM employees WHERE salary <> NULL")["rows"] == []
    assert e.execute("AAYUSH SELECT id FROM employees WHERE NOT (salary = NULL)")["rows"] == []
    assert e.execute("AAYUSH SELECT id FROM employees WHERE NOT (salary <> NULL)")["rows"] == []


def test_where_not_of_unknown_is_unknown_not_true():
    e = _seed_employees()
    # Kabir and Tara have NULL department: their NOT (department = '...') is UNKNOWN.
    assert e.execute("AAYUSH SELECT id FROM employees WHERE NOT (department = 'Engineering')")["rows"] == []
    assert e.execute("AAYUSH SELECT id FROM employees WHERE department <> 'Engineering'")["rows"] == []


def test_where_and_or_with_unknown_operands():
    e = _seed_employees()
    # TRUE OR UNKNOWN is TRUE.
    assert e.execute("AAYUSH SELECT id FROM employees WHERE id = 1 OR salary = NULL")["rows"] == [
        {"id": 1}
    ]
    # TRUE AND UNKNOWN is UNKNOWN, so even the matching row is filtered out.
    assert e.execute("AAYUSH SELECT id FROM employees WHERE id = 1 AND salary = NULL")["rows"] == []
    # FALSE AND UNKNOWN is FALSE -> no rows.
    assert e.execute("AAYUSH SELECT id FROM employees WHERE id = 99 AND salary = NULL")["rows"] == []


def test_where_unknown_keeps_ordinary_rows_matching():
    e = _seed_employees()
    res = e.execute("AAYUSH SELECT id FROM employees WHERE salary > 50000 ORDER BY id ASC")
    assert res["rows"] == [{"id": 1}]


# --- JOIN ON ---


def _seed_join_tables() -> Engine:
    e = Engine()
    e.execute("AAYUSH CREATE TABLE a (k INT)")
    e.execute("AAYUSH CREATE TABLE b (k INT, tag TEXT)")
    e.execute("AAYUSH INSERT INTO a VALUES (NULL), (1)")
    e.execute("AAYUSH INSERT INTO b VALUES (NULL, 'null_tag'), (1, 'one')")
    return e


def test_join_on_null_never_matches_even_between_nulls():
    e = _seed_join_tables()
    res = e.execute("AAYUSH SELECT a.k AS ak, b.tag AS btag FROM a INNER JOIN b ON a.k = b.k")
    assert res["rows"] == [{"ak": 1, "btag": "one"}]


def test_left_join_unknown_condition_preserves_left_row_with_nulls():
    e = _seed_join_tables()
    res = e.execute(
        "AAYUSH SELECT a.k AS ak, b.tag AS btag FROM a LEFT JOIN b ON NOT (a.k = b.k) "
        "ORDER BY a.k ASC"
    )
    # UNKNOWN is not TRUE, so no row matches and both left rows are NULL-extended
    # (NULL sort keys come last in ASC).
    assert res["rows"] == [{"ak": 1, "btag": None}, {"ak": None, "btag": None}]


# --- HAVING ---


def _seed_sales() -> Engine:
    e = Engine()
    e.execute("AAYUSH CREATE TABLE sales (dept TEXT, amount INT)")
    e.execute("AAYUSH INSERT INTO sales VALUES ('A', NULL), ('A', NULL), ('B', 10)")
    return e


def test_having_null_aggregate_result_is_not_true():
    e = _seed_sales()
    # Department A sums to NULL -> comparison is UNKNOWN -> group filtered out.
    assert e.execute(
        "AAYUSH SELECT dept, SUM(amount) AS total FROM sales GROUP BY dept HAVING SUM(amount) > 5"
    )["rows"] == [{"dept": "B", "total": 10}]
    assert e.execute(
        "AAYUSH SELECT dept, SUM(amount) AS total FROM sales GROUP BY dept HAVING SUM(amount) = NULL"
    )["rows"] == []


def test_having_not_of_unknown_keeps_group_filtered():
    e = _seed_sales()
    # A: UNKNOWN -> NOT UNKNOWN -> UNKNOWN. B: TRUE -> FALSE. Neither survives.
    assert e.execute(
        "AAYUSH SELECT dept FROM sales GROUP BY dept HAVING NOT (SUM(amount) > 5)"
    )["rows"] == []


# --- UPDATE / DELETE PREDICATES ---


def test_update_and_delete_treat_unknown_as_not_matching():
    e = _seed_employees()
    # Every row evaluates to UNKNOWN, so no row is updated or deleted.
    upd = e.execute("AAYUSH UPDATE employees SET salary = 1 WHERE NOT (department = NULL)")
    assert upd["rows_affected"] == 0

    dlt = e.execute("AAYUSH DELETE FROM employees WHERE department = NULL")
    assert dlt["rows_affected"] == 0
    assert len(e.execute("AAYUSH SELECT id FROM employees")["rows"]) == 3


def test_update_still_matches_rows_whose_predicate_is_true():
    e = _seed_employees()
    # Engineering -> FALSE -> NOT -> TRUE: only that row is updated.
    upd = e.execute("AAYUSH UPDATE employees SET salary = 1 WHERE NOT (department = 'Sales')")
    assert upd["rows_affected"] == 1


# --- DUPLICATE OUTPUT LABELS ---


def _seed_store() -> Engine:
    e = Engine()
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)")
    e.execute("AAYUSH CREATE TABLE orders (id INT PRIMARY KEY, user_id INT NOT NULL, product TEXT NOT NULL)")
    e.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush'), (2, 'Alex')")
    e.execute("AAYUSH INSERT INTO orders VALUES (10, 1, 'Laptop'), (11, 2, 'Mouse')")
    return e


def test_duplicate_alias_is_rejected_instead_of_overwriting():
    e = _seed_store()
    with pytest.raises(ExecutionError, match=r"Duplicate output column name 'identifier'"):
        e.execute(
            """
            AAYUSH SELECT u.id AS identifier, o.id AS identifier
            FROM users u
            INNER JOIN orders o ON u.id = o.user_id
            """
        )


def test_duplicate_projection_names_are_rejected():
    e = _seed_store()
    with pytest.raises(ExecutionError, match=r"Duplicate output column name 'u\.id'"):
        e.execute("AAYUSH SELECT u.id, u.id FROM users u")
    with pytest.raises(ExecutionError, match=r"Duplicate output column name 'id'"):
        e.execute("AAYUSH SELECT id, id FROM users")
    with pytest.raises(ExecutionError, match="Duplicate output column name"):
        e.execute("AAYUSH SELECT COUNT(*) AS c, SUM(id) AS c FROM users")


def test_duplicate_labels_rejected_even_when_query_would_return_no_rows():
    e = _seed_store()
    with pytest.raises(ExecutionError, match="Duplicate output column name"):
        e.execute("AAYUSH SELECT id AS x, id AS x FROM users WHERE id = 999")


def test_distinct_aliases_and_unique_labels_still_work():
    e = _seed_store()
    res = e.execute(
        """
        AAYUSH SELECT u.id AS user_id, o.id AS order_id
        FROM users u
        INNER JOIN orders o ON u.id = o.user_id
        ORDER BY order_id ASC
        """
    )
    assert res["columns"] == ["user_id", "order_id"]
    assert res["rows"] == [
        {"user_id": 1, "order_id": 10},
        {"user_id": 2, "order_id": 11},
    ]

    qualified = e.execute(
        """
        AAYUSH SELECT u.id, o.id
        FROM users u
        INNER JOIN orders o ON u.id = o.user_id
        ORDER BY o.id ASC
        """
    )
    assert qualified["columns"] == ["u.id", "o.id"]
    assert qualified["rows"][0] == {"u.id": 1, "o.id": 10}
