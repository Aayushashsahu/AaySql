import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql import Engine
from aaysql.errors import ConstraintError, ExecutionError, ParserError, StorageError
from aaysql.storage import FORMAT_VERSION


def _seed(db_path: Path | None = None) -> Engine:
    e = Engine(db_path)
    e.execute(
        """
        AAYUSH CREATE TABLE users (
            id INT PRIMARY KEY,
            name TEXT NOT NULL,
            age INT,
            city TEXT
        )
        """
    )
    e.execute(
        """
        AAYUSH INSERT INTO users VALUES
            (1, 'Aayush', 19, 'Pune'),
            (2, 'Alex', 20, 'Delhi'),
            (3, 'Mika', 19, 'Tokyo'),
            (4, 'Neha', NULL, 'Pune'),
            (5, 'Rohan', 30, 'Delhi')
        """
    )
    return e


# --- index DDL ---


def test_create_and_drop_index():
    e = _seed()
    created = e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    assert created["message"] == "Index 'idx_users_age' created on users(age)"
    assert "idx_users_age" in e.db.indexes
    assert len(e.db.indexes["idx_users_age"]) == 4  # the NULL age is not indexed

    dropped = e.execute("AAYUSH DROP INDEX idx_users_age")
    assert dropped["message"] == "Index 'idx_users_age' dropped"
    assert e.db.indexes == {}


def test_index_definition_errors():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    with pytest.raises(ExecutionError, match="already exists"):
        e.execute("AAYUSH CREATE INDEX idx_users_age ON users (id)")
    with pytest.raises(ExecutionError, match="does not exist"):
        e.execute("AAYUSH CREATE INDEX idx_missing ON ghosts (id)")
    with pytest.raises(ExecutionError, match="Unknown column"):
        e.execute("AAYUSH CREATE INDEX idx_bad ON users (nope)")
    with pytest.raises(ExecutionError, match="does not exist"):
        e.execute("AAYUSH DROP INDEX idx_nope")
    with pytest.raises(ParserError, match="Multi-column indexes"):
        e.execute("AAYUSH CREATE INDEX idx_two ON users (age, city)")
    with pytest.raises(ExecutionError, match="Invalid index name"):
        e.db.create_index("bad name", "users", "age")


def test_drop_table_removes_its_indexes(tmp_path: Path):
    db_file = tmp_path / "drop_index.aaydb"
    with Engine(db_file) as e:
        _seed_rows(e)
        e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")

    with Engine(db_file) as e2:
        e2.execute("AAYUSH DROP TABLE users")

    with Engine(db_file) as e3:
        assert e3.db.indexes == {}
        with pytest.raises(ExecutionError, match="does not exist"):
            e3.execute("AAYUSH DROP INDEX idx_users_age")


def _seed_rows(e: Engine) -> None:
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT, city TEXT)")
    e.execute(
        "AAYUSH INSERT INTO users VALUES (1, 'Aayush', 19, 'Pune'), (2, 'Alex', 20, 'Delhi')"
    )


# --- maintenance ---


def test_index_is_maintained_by_insert_update_and_delete():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")

    e.execute("AAYUSH INSERT INTO users VALUES (6, 'Ira', 19, 'Pune')")
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 19 ORDER BY id ASC")["rows"] == [
        {"id": 1},
        {"id": 3},
        {"id": 6},
    ]

    e.execute("AAYUSH UPDATE users SET age = 21 WHERE id = 6")
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 19 ORDER BY id ASC")["rows"] == [
        {"id": 1},
        {"id": 3},
    ]
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 21")["rows"] == [{"id": 6}]

    e.execute("AAYUSH UPDATE users SET age = NULL WHERE id = 6")
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 21")["rows"] == []
    assert len(e.db.indexes["idx_users_age"]) == 4

    e.execute("AAYUSH DELETE FROM users WHERE id = 1")
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 19")["rows"] == [{"id": 3}]
    e.db.indexes["idx_users_age"].validate()


def test_failed_mutations_leave_indexes_consistent():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")

    with pytest.raises(ConstraintError):
        e.execute("AAYUSH INSERT INTO users VALUES (1, 'Duplicate', 55, 'Pune')")
    with pytest.raises(ConstraintError):
        e.execute("AAYUSH UPDATE users SET id = 99")
    with pytest.raises(ConstraintError):
        e.execute("AAYUSH UPDATE users SET age = NULL, name = NULL WHERE id = 1")

    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 55")["rows"] == []
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 19 ORDER BY id ASC")["rows"] == [
        {"id": 1},
        {"id": 3},
    ]
    e.db.indexes["idx_users_age"].validate()


def test_save_failure_rolls_back_indexes_with_the_rows(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "rollback_index.aaydb"
    e = _seed(db_file)
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")

    def broken_save(_db, _changes=None):
        raise StorageError("disk full")

    monkeypatch.setattr(e.storage, "save", broken_save)
    with pytest.raises(StorageError, match="disk full"):
        e.execute("AAYUSH INSERT INTO users VALUES (7, 'Ghost', 19, 'Pune')")

    # The rolled-back row must not linger in the index either.
    assert e.execute("AAYUSH SELECT id FROM users WHERE age = 19 ORDER BY id ASC")["rows"] == [
        {"id": 1},
        {"id": 3},
    ]
    e.db.indexes["idx_users_age"].validate()


# --- planner equivalence ---


@pytest.mark.parametrize(
    "sql",
    [
        "AAYUSH SELECT * FROM users WHERE age = 19",
        "AAYUSH SELECT * FROM users WHERE age > 19",
        "AAYUSH SELECT * FROM users WHERE age >= 19",
        "AAYUSH SELECT * FROM users WHERE age < 20",
        "AAYUSH SELECT * FROM users WHERE age <= 19",
        "AAYUSH SELECT * FROM users WHERE 19 = age",
        "AAYUSH SELECT * FROM users WHERE 19 < age",
        "AAYUSH SELECT * FROM users WHERE age >= 19 AND city = 'Pune'",
        "AAYUSH SELECT * FROM users WHERE city = 'Pune' OR age = 30",
        "AAYUSH SELECT * FROM users WHERE NOT (age = 19)",
        "AAYUSH SELECT * FROM users WHERE age = NULL",
        "AAYUSH SELECT * FROM users WHERE age = 'abc'",
        "AAYUSH SELECT name, age FROM users WHERE age > 18 ORDER BY age DESC, name ASC",
        "AAYUSH SELECT COUNT(*) AS c, SUM(age) AS total FROM users WHERE age >= 19",
        "AAYUSH SELECT city, COUNT(*) AS c FROM users WHERE age >= 19 GROUP BY city HAVING COUNT(*) >= 1",
        "AAYUSH SELECT * FROM users WHERE age >= 19 ORDER BY id ASC LIMIT 2",
        "AAYUSH SELECT * FROM users WHERE age <> 19",
    ],
)
def test_indexed_and_unindexed_results_are_identical(sql: str):
    plain = _seed()
    baseline = plain.execute(sql)

    indexed = _seed()
    indexed.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    with_index = indexed.execute(sql)

    assert with_index["columns"] == baseline["columns"]
    assert with_index["rows"] == baseline["rows"]


def test_join_queries_are_not_claimed_as_index_scans():
    e = _seed()
    e.execute("AAYUSH CREATE TABLE orders (id INT PRIMARY KEY, user_id INT NOT NULL, product TEXT)")
    e.execute("AAYUSH INSERT INTO orders VALUES (10, 1, 'Laptop')")
    e.execute("AAYUSH CREATE INDEX idx_orders_user ON orders (user_id)")
    plan = e.execute("AAYUSH EXPLAIN SELECT u.name, o.product FROM users u INNER JOIN orders o ON u.id = o.user_id")
    assert "TABLE SCAN: users" in plan["message"]
    assert "JOIN: NESTED LOOP" in plan["message"]
    assert "INDEX SCAN" not in plan["message"]


def test_index_scan_returns_duplicate_keys_and_ordered_candidates():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    res = e.execute("AAYUSH SELECT id FROM users WHERE age = 19")
    assert sorted(row["id"] for row in res["rows"]) == [1, 3]
    ordered = e.execute("AAYUSH SELECT id FROM users WHERE age > 18 ORDER BY age ASC, id ASC")
    assert ordered["rows"] == [{"id": 1}, {"id": 3}, {"id": 2}, {"id": 5}]


# --- EXPLAIN ---


def test_explain_reports_index_scan_without_fabrication():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    plan = e.execute("AAYUSH EXPLAIN SELECT * FROM users WHERE age = 19")["message"]
    assert plan.startswith("QUERY PLAN")
    assert "INDEX SCAN: idx_users_age" in plan
    assert "  TABLE: users" in plan
    assert "  CONDITION: age = 19" in plan

    analyzed = e.execute("AAYUSH EXPLAIN ANALYZE SELECT * FROM users WHERE age = 19")["message"]
    assert "INDEX USED: idx_users_age" in analyzed
    # Only two rows carry age = 19; the index scan must not report a full table scan.
    assert "ROWS SCANNED: 2" in analyzed
    assert "ROWS RETURNED: 2" in analyzed
    assert "EXECUTION TIME:" in analyzed


def test_explain_reports_table_scan_and_filter():
    e = _seed()
    plan = e.execute("AAYUSH EXPLAIN SELECT * FROM users WHERE age > 18")["message"]
    assert "TABLE SCAN: users" in plan
    assert "  FILTER: age > 18" in plan
    assert "INDEX SCAN" not in plan

    analyzed = e.execute("AAYUSH EXPLAIN ANALYZE SELECT * FROM users WHERE age > 18")["message"]
    assert "ROWS SCANNED: 5" in analyzed
    assert "ROWS RETURNED: 4" in analyzed


def test_explain_shows_full_filter_when_index_covers_one_conjunct():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    plan = e.execute(
        "AAYUSH EXPLAIN SELECT * FROM users WHERE age >= 19 AND city = 'Pune'"
    )["message"]
    assert "INDEX SCAN: idx_users_age" in plan
    assert "CONDITION: age >= 19" in plan
    assert "FILTER: age >= 19 AND city = 'Pune'" in plan


def test_explain_rejects_non_select_statements():
    e = _seed()
    with pytest.raises(ExecutionError, match="supports SELECT"):
        e.execute("AAYUSH EXPLAIN CREATE TABLE t (id INT)")
    with pytest.raises(ParserError, match="requires a statement"):
        e.execute("AAYUSH EXPLAIN")


def test_explain_without_index_after_drop():
    e = _seed()
    e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")
    e.execute("AAYUSH DROP INDEX idx_users_age")
    plan = e.execute("AAYUSH EXPLAIN SELECT * FROM users WHERE age = 19")["message"]
    assert "TABLE SCAN: users" in plan


# --- persistence and migration ---


def test_indexes_survive_reopen_and_keep_accelerating_queries(tmp_path: Path):
    db_file = tmp_path / "indexed.aaydb"
    with Engine(db_file) as e:
        _seed_rows(e)
        e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")

    with Engine(db_file) as e2:
        assert "idx_users_age" in e2.db.indexes
        assert len(e2.db.indexes["idx_users_age"]) == 2
        plan = e2.execute("AAYUSH EXPLAIN SELECT * FROM users WHERE age = 20")["message"]
        assert "INDEX SCAN: idx_users_age" in plan
        assert e2.execute("AAYUSH SELECT name FROM users WHERE age = 20")["rows"] == [
            {"name": "Alex"}
        ]
        e2.execute("AAYUSH INSERT INTO users VALUES (3, 'Mika', 20, 'Tokyo')")

    with Engine(db_file) as e3:
        assert e3.execute("AAYUSH SELECT name FROM users WHERE age = 20 ORDER BY name ASC")["rows"] == [
            {"name": "Alex"},
            {"name": "Mika"},
        ]


def test_version_1_database_file_migrates_to_version_2(tmp_path: Path):
    db_file = tmp_path / "legacy.aaydb"
    v1_payload = {
        "format": "aaysql",
        "format_version": 1,
        "engine_version": "0.3.0",
        "tables": {
            "users": {
                "name": "users",
                "columns": [
                    {
                        "name": "id",
                        "type_name": "INT",
                        "primary_key": True,
                        "not_null": False,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    },
                    {
                        "name": "age",
                        "type_name": "INT",
                        "primary_key": False,
                        "not_null": False,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    },
                ],
                "rows": [{"id": 1, "age": 19}, {"id": 2, "age": None}],
            }
        },
    }
    db_file.write_text(json.dumps(v1_payload), encoding="utf-8")

    with Engine(db_file) as e:
        assert e.execute("AAYUSH SELECT id FROM users ORDER BY id ASC")["rows"] == [
            {"id": 1},
            {"id": 2},
        ]
        e.execute("AAYUSH CREATE INDEX idx_users_age ON users (age)")

    payload = json.loads(db_file.read_text(encoding="utf-8"))
    assert payload["format_version"] == FORMAT_VERSION == 3
    assert payload["indexes"] == {"idx_users_age": {"table": "users", "column": "age"}}

    with Engine(db_file) as e2:
        assert e2.execute("AAYUSH SELECT id FROM users WHERE age = 19")["rows"] == [{"id": 1}]


@pytest.mark.parametrize(
    "indexes",
    [
        {"idx": "not-a-dict"},
        {"idx": {"table": "missing", "column": "age"}},
        {"idx": {"table": "users", "column": "nope"}},
        {"idx": {"table": "users"}},
        {"bad name": {"table": "users", "column": "age"}},
    ],
)
def test_corrupt_index_metadata_is_rejected_without_touching_the_file(
    tmp_path: Path, indexes: dict
):
    db_file = tmp_path / "corrupt_index.aaydb"
    payload = {
        "format": "aaysql",
        "format_version": 2,
        "engine_version": "0.4.0",
        "tables": {
            "users": {
                "name": "users",
                "columns": [
                    {
                        "name": "age",
                        "type_name": "INT",
                        "primary_key": False,
                        "not_null": False,
                        "unique": False,
                        "default": None,
                        "has_default": False,
                    }
                ],
                "rows": [{"age": 19}],
            }
        },
        "indexes": indexes,
    }
    original = json.dumps(payload)
    db_file.write_text(original, encoding="utf-8")

    with pytest.raises(StorageError):
        Engine(db_file)
    assert db_file.read_text(encoding="utf-8") == original


def test_unknown_format_version_is_rejected(tmp_path: Path):
    db_file = tmp_path / "future.aaydb"
    db_file.write_text(
        json.dumps({"format": "aaysql", "format_version": 99, "tables": {}, "indexes": {}}),
        encoding="utf-8",
    )
    with pytest.raises(StorageError, match="Unsupported database format version"):
        Engine(db_file)
