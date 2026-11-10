import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql import Engine
from aaysql.errors import ExecutionError, ParserError


def _seed_store_db(db_path: Path | None = None) -> Engine:
    e = Engine(db_path)
    e.execute(
        """
        AAYUSH CREATE TABLE users (
            id INT PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT
        )
        """
    )
    e.execute(
        """
        AAYUSH CREATE TABLE orders (
            id INT PRIMARY KEY,
            user_id INT NOT NULL,
            product TEXT NOT NULL,
            price FLOAT NOT NULL
        )
        """
    )
    e.execute(
        """
        AAYUSH CREATE TABLE shipments (
            id INT PRIMARY KEY,
            order_id INT NOT NULL,
            carrier TEXT NOT NULL
        )
        """
    )
    e.execute(
        """
        AAYUSH INSERT INTO users VALUES
            (1, 'Aayush', 'Pune'),
            (2, 'Alex', 'Delhi'),
            (3, 'Mika', 'Tokyo')
        """
    )
    e.execute(
        """
        AAYUSH INSERT INTO orders VALUES
            (10, 1, 'Laptop', 1200.0),
            (11, 1, 'Mouse', 25.0),
            (12, 2, 'Keyboard', 75.0)
        """
    )
    e.execute(
        """
        AAYUSH INSERT INTO shipments VALUES
            (100, 10, 'BlueDart'),
            (101, 12, 'DHL')
        """
    )
    return e


def _seed_employees_db() -> Engine:
    e = Engine()
    e.execute(
        """
        AAYUSH CREATE TABLE employees (
            id INT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT,
            city TEXT,
            salary INT
        )
        """
    )
    e.execute(
        """
        AAYUSH INSERT INTO employees VALUES
            (1, 'Asha', 'Engineering', 'Pune', 80000),
            (2, 'Vikram', 'Engineering', 'Pune', 90000),
            (3, 'Neha', 'Engineering', 'Bangalore', 70000),
            (4, 'Rohan', 'Sales', 'Delhi', 45000),
            (5, 'Meera', 'Sales', 'Delhi', 55000),
            (6, 'Kabir', NULL, 'Pune', 40000),
            (7, 'Tara', NULL, 'Pune', NULL)
        """
    )
    return e


# --- JOIN TESTS ---


def test_inner_join_single_and_multiple_matches():
    e = _seed_store_db()
    res = e.execute(
        """
        AAYUSH SELECT users.name, orders.product
        FROM users
        INNER JOIN orders ON users.id = orders.user_id
        ORDER BY orders.id ASC
        """
    )
    assert res["columns"] == ["users.name", "orders.product"]
    assert res["rows"] == [
        {"users.name": "Aayush", "orders.product": "Laptop"},
        {"users.name": "Aayush", "orders.product": "Mouse"},
        {"users.name": "Alex", "orders.product": "Keyboard"},
    ]

    # Single matching row via WHERE
    single = e.execute(
        """
        AAYUSH SELECT users.name, orders.product
        FROM users
        INNER JOIN orders ON users.id = orders.user_id
        WHERE orders.user_id = 2
        """
    )
    assert single["rows"] == [{"users.name": "Alex", "orders.product": "Keyboard"}]


def test_inner_join_no_matches_and_empty_tables():
    e = _seed_store_db()
    res = e.execute(
        """
        AAYUSH SELECT users.name, orders.product
        FROM users
        INNER JOIN orders ON users.id = orders.id
        """
    )
    assert res["rows"] == []

    e.execute("AAYUSH CREATE TABLE empty_orders (id INT PRIMARY KEY, user_id INT)")
    res_empty = e.execute(
        """
        AAYUSH SELECT users.name, empty_orders.id
        FROM users
        INNER JOIN empty_orders ON users.id = empty_orders.user_id
        """
    )
    assert res_empty["rows"] == []


def test_left_join_matches_unmatched_and_empty_right():
    e = _seed_store_db()
    res = e.execute(
        """
        AAYUSH SELECT users.name, orders.product
        FROM users
        LEFT JOIN orders ON users.id = orders.user_id
        ORDER BY users.id ASC, orders.id ASC
        """
    )
    assert res["rows"] == [
        {"users.name": "Aayush", "orders.product": "Laptop"},
        {"users.name": "Aayush", "orders.product": "Mouse"},
        {"users.name": "Alex", "orders.product": "Keyboard"},
        {"users.name": "Mika", "orders.product": None},
    ]

    # Empty right table preserves all left rows with NULLs.
    e.execute("AAYUSH CREATE TABLE empty_orders (id INT PRIMARY KEY, user_id INT, item TEXT)")
    res_empty_right = e.execute(
        """
        AAYUSH SELECT users.name, empty_orders.item
        FROM users
        LEFT JOIN empty_orders ON users.id = empty_orders.user_id
        ORDER BY users.id ASC
        """
    )
    assert res_empty_right["rows"] == [
        {"users.name": "Aayush", "empty_orders.item": None},
        {"users.name": "Alex", "empty_orders.item": None},
        {"users.name": "Mika", "empty_orders.item": None},
    ]


def test_three_table_join():
    e = _seed_store_db()
    res = e.execute(
        """
        AAYUSH SELECT u.name AS customer, o.product AS item, s.carrier AS carrier
        FROM users u
        INNER JOIN orders o ON u.id = o.user_id
        LEFT JOIN shipments s ON o.id = s.order_id
        ORDER BY o.id ASC
        """
    )
    assert res["columns"] == ["customer", "item", "carrier"]
    assert res["rows"] == [
        {"customer": "Aayush", "item": "Laptop", "carrier": "BlueDart"},
        {"customer": "Aayush", "item": "Mouse", "carrier": None},
        {"customer": "Alex", "item": "Keyboard", "carrier": "DHL"},
    ]


def test_table_aliases_with_and_without_as_and_qualified_wildcard():
    e = _seed_store_db()
    r1 = e.execute(
        """
        AAYUSH SELECT u.id, o.id, u.name, o.product
        FROM users AS u
        INNER JOIN orders AS o ON u.id = o.user_id
        ORDER BY o.id ASC
        """
    )
    assert r1["columns"] == ["u.id", "o.id", "u.name", "o.product"]
    assert r1["rows"][0] == {"u.id": 1, "o.id": 10, "u.name": "Aayush", "o.product": "Laptop"}

    r2 = e.execute(
        """
        AAYUSH SELECT u.name, o.product
        FROM users u
        INNER JOIN orders o ON u.id = o.user_id
        WHERE u.id = 2
        """
    )
    assert r2["rows"] == [{"u.name": "Alex", "o.product": "Keyboard"}]

    # Wildcard across joined tables qualifies columns to avoid collisions.
    r_star = e.execute(
        """
        AAYUSH SELECT *
        FROM users u
        INNER JOIN orders o ON u.id = o.user_id
        WHERE o.id = 12
        """
    )
    assert r_star["columns"] == ["u.id", "u.name", "u.city", "o.id", "o.user_id", "o.product", "o.price"]
    assert r_star["rows"] == [
        {"u.id": 2, "u.name": "Alex", "u.city": "Delhi", "o.id": 12, "o.user_id": 2, "o.product": "Keyboard", "o.price": 75.0}
    ]


def test_join_resolution_errors_and_join_condition_types():
    e = _seed_store_db()
    # Unknown table alias
    with pytest.raises(ExecutionError, match="Unknown table or alias 'x'"):
        e.execute("AAYUSH SELECT x.id FROM users u INNER JOIN orders o ON u.id = o.user_id")

    # Unknown column on valid alias
    with pytest.raises(ExecutionError, match="Unknown column 'u.nope'"):
        e.execute("AAYUSH SELECT u.nope FROM users u INNER JOIN orders o ON u.id = o.user_id")

    # Ambiguous unqualified column
    with pytest.raises(ExecutionError, match="Ambiguous column reference 'id'"):
        e.execute("AAYUSH SELECT id FROM users INNER JOIN orders ON users.id = orders.user_id")

    with pytest.raises(ExecutionError, match="Ambiguous column reference 'id'"):
        e.execute("AAYUSH SELECT users.name FROM users INNER JOIN orders ON users.id = orders.user_id WHERE id = 1")

    # Duplicate alias rejected
    with pytest.raises(ExecutionError, match="Duplicate table name or alias"):
        e.execute("AAYUSH SELECT u.id FROM users u INNER JOIN orders u ON u.id = u.user_id")

    # Incompatible ordering comparison in JOIN ON raises ExecutionError
    with pytest.raises(ExecutionError, match="Cannot compare"):
        e.execute("AAYUSH SELECT u.name FROM users u INNER JOIN orders o ON u.id > o.product")

    # NULLs do not match in ON equality
    e.execute("AAYUSH CREATE TABLE a (k INT)")
    e.execute("AAYUSH CREATE TABLE b (k INT)")
    e.execute("AAYUSH INSERT INTO a VALUES (NULL)")
    e.execute("AAYUSH INSERT INTO b VALUES (NULL)")
    assert e.execute("AAYUSH SELECT a.k FROM a INNER JOIN b ON a.k = b.k")["rows"] == []

    # Unsupported JOIN type rejected by parser
    with pytest.raises(ParserError, match="Unsupported JOIN type"):
        e.execute("AAYUSH SELECT u.id FROM users u RIGHT JOIN orders o ON u.id = o.user_id")


# --- AGGREGATION TESTS ---


def test_aggregates_count_sum_avg_min_max_and_aliases():
    e = _seed_employees_db()
    res = e.execute(
        """
        AAYUSH SELECT
            COUNT(*) AS total_rows,
            COUNT(salary) AS non_null_salaries,
            COUNT(department) AS non_null_depts,
            SUM(salary) AS total_salary,
            AVG(salary) AS avg_salary,
            MIN(salary) AS min_salary,
            MAX(salary) AS max_salary
        FROM employees
        """
    )
    assert res["columns"] == [
        "total_rows",
        "non_null_salaries",
        "non_null_depts",
        "total_salary",
        "avg_salary",
        "min_salary",
        "max_salary",
    ]
    row = res["rows"][0]
    assert row["total_rows"] == 7
    assert row["non_null_salaries"] == 6
    assert row["non_null_depts"] == 5
    assert row["total_salary"] == 380000
    assert row["avg_salary"] == pytest.approx(380000 / 6)
    assert row["min_salary"] == 40000
    assert row["max_salary"] == 90000


def test_aggregates_default_column_names_and_text_min_max():
    e = _seed_employees_db()
    res = e.execute("AAYUSH SELECT COUNT(*), MIN(name), MAX(name) FROM employees")
    assert res["columns"] == ["COUNT(*)", "MIN(name)", "MAX(name)"]
    assert res["rows"] == [{"COUNT(*)": 7, "MIN(name)": "Asha", "MAX(name)": "Vikram"}]


def test_aggregates_empty_input_and_all_null_column():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE nums (id INT PRIMARY KEY, val INT, label TEXT)")
    empty_res = e.execute(
        "AAYUSH SELECT COUNT(*) AS c_all, COUNT(val) AS c_val, SUM(val) AS s, AVG(val) AS a, MIN(val) AS mn, MAX(val) AS mx FROM nums"
    )
    assert empty_res["rows"] == [{"c_all": 0, "c_val": 0, "s": None, "a": None, "mn": None, "mx": None}]

    e.execute("AAYUSH INSERT INTO nums VALUES (1, NULL, 'x'), (2, NULL, 'y')")
    null_res = e.execute(
        "AAYUSH SELECT COUNT(*) AS c_all, COUNT(val) AS c_val, SUM(val) AS s, AVG(val) AS a, MIN(val) AS mn, MAX(val) AS mx FROM nums"
    )
    assert null_res["rows"] == [{"c_all": 2, "c_val": 0, "s": None, "a": None, "mn": None, "mx": None}]


def test_aggregates_invalid_arguments_and_placement():
    e = _seed_employees_db()
    with pytest.raises(ExecutionError, match="numeric"):
        e.execute("AAYUSH SELECT SUM(name) FROM employees")
    with pytest.raises(ExecutionError, match="numeric"):
        e.execute("AAYUSH SELECT AVG(name) FROM employees")
    with pytest.raises(ParserError, match="only COUNT"):
        e.execute("AAYUSH SELECT SUM(*) FROM employees")
    with pytest.raises(ExecutionError, match="Nested aggregate"):
        e.execute("AAYUSH SELECT SUM(COUNT(*)) FROM employees")
    with pytest.raises(ExecutionError, match="WHERE"):
        e.execute("AAYUSH SELECT * FROM employees WHERE COUNT(*) > 1")
    with pytest.raises(ParserError, match="Unsupported function"):
        e.execute("AAYUSH SELECT MEDIAN(salary) FROM employees")


# --- GROUP BY & HAVING TESTS ---


def test_group_by_single_and_multiple_columns_with_null_keys():
    e = _seed_employees_db()
    single = e.execute(
        """
        AAYUSH SELECT department, COUNT(*) AS cnt, COUNT(salary) AS sal_cnt, SUM(salary) AS total
        FROM employees
        GROUP BY department
        ORDER BY cnt DESC, department ASC
        """
    )
    assert single["rows"] == [
        {"department": "Engineering", "cnt": 3, "sal_cnt": 3, "total": 240000},
        {"department": "Sales", "cnt": 2, "sal_cnt": 2, "total": 100000},
        {"department": None, "cnt": 2, "sal_cnt": 1, "total": 40000},
    ]

    multi = e.execute(
        """
        AAYUSH SELECT department, city, COUNT(*) AS cnt
        FROM employees
        GROUP BY department, city
        ORDER BY cnt DESC, city ASC
        """
    )
    assert len(multi["rows"]) == 4
    assert {"department": "Engineering", "city": "Pune", "cnt": 2} in multi["rows"]
    assert {"department": None, "city": "Pune", "cnt": 2} in multi["rows"]


def test_group_by_empty_input_and_where_interaction():
    e = _seed_employees_db()
    res = e.execute(
        """
        AAYUSH SELECT department, COUNT(*) AS cnt, AVG(salary) AS avg_sal
        FROM employees
        WHERE city = 'Pune'
        GROUP BY department
        ORDER BY cnt DESC
        """
    )
    assert res["rows"] == [
        {"department": "Engineering", "cnt": 2, "avg_sal": 85000.0},
        {"department": None, "cnt": 2, "avg_sal": 40000.0},
    ]

    empty = e.execute(
        """
        AAYUSH SELECT department, COUNT(*) AS cnt
        FROM employees
        WHERE city = 'Nowhere'
        GROUP BY department
        """
    )
    assert empty["rows"] == []


def test_group_by_rejects_ungrouped_selected_columns():
    e = _seed_employees_db()
    with pytest.raises(ExecutionError, match="GROUP BY"):
        e.execute("AAYUSH SELECT department, name, COUNT(*) FROM employees GROUP BY department")

    with pytest.raises(ExecutionError, match="GROUP BY"):
        e.execute("AAYUSH SELECT department, COUNT(*) FROM employees")

    with pytest.raises(ExecutionError, match="GROUP BY"):
        e.execute("AAYUSH SELECT * FROM employees GROUP BY department")

    with pytest.raises(ExecutionError, match="GROUP BY"):
        e.execute("AAYUSH SELECT department, COUNT(*) FROM employees GROUP BY department HAVING salary > 50000")


def test_having_count_sum_avg_multiple_conditions_and_empty():
    e = _seed_employees_db()

    # COUNT-based HAVING
    r_cnt = e.execute(
        """
        AAYUSH SELECT department, COUNT(*) AS employee_count
        FROM employees
        GROUP BY department
        HAVING COUNT(*) > 2
        """
    )
    assert r_cnt["rows"] == [{"department": "Engineering", "employee_count": 3}]

    # SUM-based HAVING
    r_sum = e.execute(
        """
        AAYUSH SELECT department, SUM(salary) AS total_sal
        FROM employees
        GROUP BY department
        HAVING SUM(salary) >= 100000
        ORDER BY total_sal DESC
        """
    )
    assert r_sum["rows"] == [
        {"department": "Engineering", "total_sal": 240000},
        {"department": "Sales", "total_sal": 100000},
    ]

    # AVG-based HAVING + ORDER BY alias
    r_avg = e.execute(
        """
        AAYUSH SELECT department, AVG(salary) AS average_salary
        FROM employees
        GROUP BY department
        HAVING AVG(salary) > 50000
        ORDER BY average_salary DESC
        """
    )
    assert r_avg["rows"] == [{"department": "Engineering", "average_salary": 80000.0}]

    # Multiple HAVING conditions + WHERE + GROUP BY
    r_multi = e.execute(
        """
        AAYUSH SELECT department, COUNT(*) AS cnt, AVG(salary) AS avg_sal
        FROM employees
        WHERE salary > 40000
        GROUP BY department
        HAVING COUNT(*) >= 2 AND AVG(salary) >= 50000
        ORDER BY avg_sal ASC
        """
    )
    assert r_multi["rows"] == [
        {"department": "Sales", "cnt": 2, "avg_sal": 50000.0},
        {"department": "Engineering", "cnt": 3, "avg_sal": 80000.0},
    ]

    # Empty HAVING result
    r_none = e.execute(
        """
        AAYUSH SELECT department, COUNT(*) AS cnt
        FROM employees
        GROUP BY department
        HAVING COUNT(*) > 100
        """
    )
    assert r_none["rows"] == []


# --- COMBINED QUERIES & PERSISTENCE ---


def test_combined_join_where_group_by_having_order_by_limit(tmp_path: Path):
    db_file = tmp_path / "store.aaydb"
    with _seed_store_db(db_file) as e1:
        pass

    before_bytes = db_file.read_bytes()

    with Engine(db_file) as e2:
        # LEFT JOIN + GROUP BY + COUNT(o.id) (0 for Mika!) + ORDER BY + LIMIT
        res_all = e2.execute(
            """
            AAYUSH SELECT
                u.name AS customer,
                COUNT(o.id) AS order_count,
                SUM(o.price) AS total_spent
            FROM users u
            LEFT JOIN orders o ON u.id = o.user_id
            GROUP BY u.id, u.name
            ORDER BY order_count DESC, u.name ASC
            LIMIT 3
            """
        )
        assert res_all["rows"] == [
            {"customer": "Aayush", "order_count": 2, "total_spent": 1225.0},
            {"customer": "Alex", "order_count": 1, "total_spent": 75.0},
            {"customer": "Mika", "order_count": 0, "total_spent": None},
        ]

        # JOIN + WHERE + GROUP BY + HAVING + ORDER BY + LIMIT
        res_filtered = e2.execute(
            """
            AAYUSH SELECT
                u.name AS customer,
                COUNT(o.id) AS order_count,
                SUM(o.price) AS total_spent
            FROM users AS u
            INNER JOIN orders AS o ON u.id = o.user_id
            WHERE o.price >= 25.0
            GROUP BY u.id, u.name
            HAVING SUM(o.price) > 50.0
            ORDER BY total_spent DESC
            LIMIT 1
            """
        )
        assert res_filtered["rows"] == [
            {"customer": "Aayush", "order_count": 2, "total_spent": 1225.0}
        ]

    # Relational SELECT queries must remain read-only on disk.
    assert db_file.read_bytes() == before_bytes


def test_aayush_prefix_still_mandatory_for_joins_and_aggregates():
    e = _seed_store_db()
    with pytest.raises(ParserError):
        e.execute("SELECT u.name, o.product FROM users u INNER JOIN orders o ON u.id = o.user_id")
    with pytest.raises(ParserError):
        e.execute("SELECT COUNT(*) FROM users")
    with pytest.raises(ParserError, match="Duplicate AAYUSH"):
        e.execute("AAYUSH AAYUSH SELECT COUNT(*) FROM users")
