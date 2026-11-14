import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql import Engine
from aaysql.ast import BinaryExpr, ColumnDef, Identifier, Literal, Select, SelectItem
from aaysql.errors import (
    AayushSQLError,
    ConstraintError,
    ExecutionError,
    ParserError,
    StorageError,
)
from aaysql.storage import Table


# --- ISSUE 1: UPDATE assignment grammar ---


def _setup_users_engine() -> Engine:
    e = Engine()
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)")
    e.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush', 19), (2, 'Alex', 20)")
    return e


def test_update_valid_single_and_multi_column():
    e = _setup_users_engine()
    res1 = e.execute("AAYUSH UPDATE users SET age = 21 WHERE id = 1")
    assert res1["rows_affected"] == 1
    res2 = e.execute("AAYUSH UPDATE users SET name = 'Alexander', age = 22 WHERE id = 2")
    assert res2["rows_affected"] == 1
    rows = e.execute("AAYUSH SELECT * FROM users ORDER BY id ASC")["rows"]
    assert rows == [
        {"id": 1, "name": "Aayush", "age": 21},
        {"id": 2, "name": "Alexander", "age": 22},
    ]


def test_update_can_copy_column_value_and_set_null():
    e = _setup_users_engine()
    e.execute("AAYUSH UPDATE users SET age = NULL WHERE id = 1")
    assert e.execute("AAYUSH SELECT age FROM users WHERE id = 1")["rows"] == [{"age": None}]
    e.execute("AAYUSH UPDATE users SET age = id WHERE id = 2")
    assert e.execute("AAYUSH SELECT age FROM users WHERE id = 2")["rows"] == [{"age": 2}]


@pytest.mark.parametrize(
    "sql",
    [
        "AAYUSH UPDATE users SET age 20 WHERE id = 1",
        "AAYUSH UPDATE users SET age = = 20 WHERE id = 1",
        "AAYUSH UPDATE users SET age == 20 WHERE id = 1",
        "AAYUSH UPDATE users SET age = WHERE id = 1",
        "AAYUSH UPDATE users SET age = WHERE",
        "AAYUSH UPDATE users SET age =;",
        "AAYUSH UPDATE users SET = 20 WHERE id = 1",
        "AAYUSH UPDATE users SET age = 20, WHERE id = 1",
        "AAYUSH UPDATE users SET age = 20, name 'Bob' WHERE id = 1",
        "AAYUSH UPDATE users SET age = 20 name = 'Bob' WHERE id = 1",
        "AAYUSH UPDATE users SET age = 20,;",
    ],
)
def test_update_rejects_malformed_assignments(sql: str):
    e = _setup_users_engine()
    with pytest.raises(ParserError) as exc_info:
        e.execute(sql)
    assert "position" in str(exc_info.value)
    # Database state must remain unchanged.
    assert e.execute("AAYUSH SELECT age FROM users WHERE id = 1")["rows"] == [{"age": 19}]


# --- ISSUE 2: CREATE TABLE schema & default validation ---


@pytest.mark.parametrize(
    ("type_sql", "sample_val", "expected_val"),
    [
        ("INT", 7, 7),
        ("INTEGER", -3, -3),
        ("TEXT", "hello", "hello"),
        ("VARCHAR", "world", "world"),
        ("FLOAT", 2, 2.0),
        ("DOUBLE", 3.5, 3.5),
        ("BOOL", 1, True),
        ("BOOLEAN", 0, False),
    ],
)
def test_create_table_all_supported_types_and_defaults(tmp_path: Path, type_sql: str, sample_val, expected_val):
    db_file = tmp_path / f"{type_sql.lower()}.aaydb"
    lit = f"'{sample_val}'" if isinstance(sample_val, str) else str(sample_val)
    with Engine(db_file) as e1:
        e1.execute(f"AAYUSH CREATE TABLE t (id INT PRIMARY KEY, val {type_sql} DEFAULT {lit})")
        e1.execute("AAYUSH INSERT INTO t (id) VALUES (1)")
    with Engine(db_file) as e2:
        row = e2.execute("AAYUSH SELECT val FROM t WHERE id = 1")["rows"][0]
        assert row["val"] == expected_val
        assert type(row["val"]) is type(expected_val)


@pytest.mark.parametrize("bad_type", ["UNKNOWN_TYPE", "DATE", "BLOB", "CHAR", "XML"])
def test_create_table_rejects_unsupported_types(bad_type: str):
    e = Engine()
    with pytest.raises(ParserError, match="Unsupported data type"):
        e.execute(f"AAYUSH CREATE TABLE bad (id {bad_type}, name TEXT)")
    with pytest.raises(ExecutionError, match="Unsupported data type"):
        e.db.create_table("bad", [ColumnDef("id", bad_type), ColumnDef("name", "TEXT")])


@pytest.mark.parametrize(
    "col_sql",
    [
        "x INT DEFAULT 'not_int'",
        "x INT DEFAULT 1.5",
        "x TEXT DEFAULT 123",
        "x FLOAT DEFAULT 'nope'",
        "x BOOL DEFAULT 2",
        "x BOOL DEFAULT 'true'",
        "x INT NOT NULL DEFAULT NULL",
        "x INT PRIMARY KEY DEFAULT NULL",
        "x INT DEFAULT 1 DEFAULT 2",
    ],
)
def test_create_table_rejects_invalid_defaults(col_sql: str):
    e = Engine()
    with pytest.raises((ConstraintError, ParserError)):
        e.execute(f"AAYUSH CREATE TABLE bad ({col_sql})")


def test_create_table_allows_default_null_on_nullable_column():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, note TEXT DEFAULT NULL, age INT DEFAULT NULL)")
    e.execute("AAYUSH INSERT INTO t (id) VALUES (1)")
    assert e.execute("AAYUSH SELECT * FROM t")["rows"] == [{"id": 1, "note": None, "age": None}]


def test_create_table_rejects_duplicate_columns_and_multiple_pks():
    e = Engine()
    with pytest.raises(ExecutionError, match="Duplicate column"):
        e.execute("AAYUSH CREATE TABLE t (id INT, ID TEXT)")
    with pytest.raises(ExecutionError, match="PRIMARY KEY"):
        e.execute("AAYUSH CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)")


@pytest.mark.parametrize(
    "bad_col",
    [
        {
            "name": "x",
            "type_name": "UNKNOWN_TYPE",
            "primary_key": False,
            "not_null": False,
            "unique": False,
            "default": None,
            "has_default": False,
        },
        {
            "name": "x",
            "type_name": "INT",
            "primary_key": False,
            "not_null": False,
            "unique": False,
            "default": "bad_int",
            "has_default": True,
        },
        {
            "name": "x",
            "type_name": "INT",
            "primary_key": False,
            "not_null": True,
            "unique": False,
            "default": None,
            "has_default": True,
        },
    ],
)
def test_storage_load_applies_same_schema_validation(tmp_path: Path, bad_col: dict):
    db_file = tmp_path / "bad_schema.aaydb"
    db_file.write_text(
        json.dumps(
            {
                "format": "aaysql",
                "format_version": 1,
                "tables": {"t": {"name": "t", "columns": [bad_col], "rows": []}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StorageError):
        Engine(db_file)


# --- ISSUE 4: Runtime exception normalization & chaining ---


@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_incompatible_ordering_comparison_raises_execution_error_with_cause(op: str):
    e = _setup_users_engine()
    with pytest.raises(ExecutionError, match="Cannot compare int and str") as exc_info:
        e.execute(f"AAYUSH SELECT * FROM users WHERE age {op} 'abc'")
    assert isinstance(exc_info.value.__cause__, TypeError)


def test_incompatible_equality_comparison_does_not_crash():
    e = _setup_users_engine()
    assert e.execute("AAYUSH SELECT * FROM users WHERE age = 'abc'")["rows"] == []
    assert len(e.execute("AAYUSH SELECT * FROM users WHERE age != 'abc'")["rows"]) == 2


def test_unknown_column_and_unsupported_operator_raise_execution_error():
    e = _setup_users_engine()
    with pytest.raises(ExecutionError, match="Unknown column"):
        e.execute("AAYUSH SELECT missing_col FROM users")
    with pytest.raises(ExecutionError, match="Unknown column"):
        e.execute("AAYUSH SELECT * FROM users WHERE missing_col = 1")
    with pytest.raises(ExecutionError, match="Unsupported operator"):
        e.executor.execute(
            Select(
                columns=[SelectItem(Identifier("*"))],
                table="users",
                where=BinaryExpr(Identifier("age"), "~", Literal(10)),
            )
        )


def test_order_by_incompatible_values_raises_execution_error():
    e = Engine()
    e.db.tables["mixed"] = Table(
        name="mixed",
        columns=[ColumnDef("v", "INT")],
        rows=[{"v": 1}, {"v": "abc"}],
    )
    with pytest.raises(ExecutionError, match="Cannot sort column"):
        e.execute("AAYUSH SELECT v FROM mixed ORDER BY v ASC")


def test_unexpected_internal_bug_is_not_swallowed(monkeypatch):
    e = _setup_users_engine()

    def internal_bug(_stmt):
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(e.executor, "execute", internal_bug)
    with pytest.raises(RuntimeError, match="unexpected internal failure"):
        e.execute("AAYUSH SELECT * FROM users")


def test_constraint_and_storage_error_hierarchy_preserved(tmp_path: Path):
    e = Engine(tmp_path / "err.aaydb")
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY)")
    e.execute("AAYUSH INSERT INTO users VALUES (1)")
    with pytest.raises(ConstraintError):
        e.execute("AAYUSH INSERT INTO users VALUES (1)")
    assert issubclass(ConstraintError, ExecutionError)
    assert issubclass(ExecutionError, AayushSQLError)
    assert issubclass(StorageError, AayushSQLError)
