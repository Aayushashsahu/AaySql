import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql import Engine
from aaysql.errors import ConstraintError, ExecutionError, ParserError, StorageError


def test_create_insert_and_reopen_persists_schema_and_rows(tmp_path: Path):
    db_file = tmp_path / "university.aaydb"

    e1 = Engine(db_file)
    e1.execute(
        """
        AAYUSH CREATE TABLE users (
            id INT PRIMARY KEY,
            name TEXT NOT NULL,
            age INT
        )
        """
    )
    e1.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush', 19)")
    e1.execute("AAYUSH INSERT INTO users VALUES (2, 'Alex', 20)")
    e1.close()

    assert db_file.exists()

    e2 = Engine(db_file)
    res = e2.execute("AAYUSH SELECT * FROM users ORDER BY id ASC")
    assert res["columns"] == ["id", "name", "age"]
    assert res["rows"] == [
        {"id": 1, "name": "Aayush", "age": 19},
        {"id": 2, "name": "Alex", "age": 20},
    ]
    e2.close()


def test_update_and_delete_persist_across_reopen(tmp_path: Path):
    db_file = tmp_path / "crud.aaydb"

    with Engine(db_file) as e1:
        e1.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, label TEXT NOT NULL, qty INT)")
        e1.execute("AAYUSH INSERT INTO items VALUES (1, 'Pen', 5), (2, 'Book', 3), (3, 'Bag', 1)")

    with Engine(db_file) as e2:
        upd = e2.execute("AAYUSH UPDATE items SET qty = 10 WHERE id = 1")
        assert upd["rows_affected"] == 1
        dlt = e2.execute("AAYUSH DELETE FROM items WHERE id = 2")
        assert dlt["rows_affected"] == 1

    with Engine(db_file) as e3:
        rows = e3.execute("AAYUSH SELECT * FROM items ORDER BY id ASC")["rows"]
        assert rows == [
            {"id": 1, "label": "Pen", "qty": 10},
            {"id": 3, "label": "Bag", "qty": 1},
        ]


def test_drop_table_persists_across_reopen(tmp_path: Path):
    db_file = tmp_path / "drop.aaydb"
    with Engine(db_file) as e1:
        e1.execute("AAYUSH CREATE TABLE temp (id INT PRIMARY KEY)")
        e1.execute("AAYUSH INSERT INTO temp VALUES (1)")
    with Engine(db_file) as e2:
        e2.execute("AAYUSH DROP TABLE temp")
    with Engine(db_file) as e3:
        with pytest.raises(ExecutionError, match="does not exist"):
            e3.execute("AAYUSH SELECT * FROM temp")


def test_preserve_column_types_defaults_and_constraints_after_reload(tmp_path: Path):
    db_file = tmp_path / "types.aaydb"
    with Engine(db_file) as e1:
        e1.execute(
            """
            AAYUSH CREATE TABLE accounts (
                id INT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                balance FLOAT DEFAULT 0.0,
                active BOOL DEFAULT 1,
                city TEXT DEFAULT 'Pune',
                note TEXT DEFAULT NULL
            )
            """
        )
        e1.execute("AAYUSH INSERT INTO accounts (id, email) VALUES (1, 'a@example.com')")

    with Engine(db_file) as e2:
        e2.execute("AAYUSH INSERT INTO accounts (id, email, balance, active) VALUES (2, 'b@example.com', 42, 0)")
        rows = e2.execute("AAYUSH SELECT * FROM accounts ORDER BY id ASC")["rows"]
        assert rows == [
            {"id": 1, "email": "a@example.com", "balance": 0.0, "active": True, "city": "Pune", "note": None},
            {"id": 2, "email": "b@example.com", "balance": 42.0, "active": False, "city": "Pune", "note": None},
        ]
        assert isinstance(rows[1]["balance"], float)
        assert isinstance(rows[1]["active"], bool)

        # Duplicate PRIMARY KEY rejected after reload
        with pytest.raises(ConstraintError, match="PRIMARY KEY"):
            e2.execute("AAYUSH INSERT INTO accounts (id, email) VALUES (1, 'c@example.com')")

        # Duplicate UNIQUE rejected after reload
        with pytest.raises(ConstraintError, match="UNIQUE"):
            e2.execute("AAYUSH INSERT INTO accounts (id, email) VALUES (3, 'a@example.com')")

        # NOT NULL rejected after reload
        with pytest.raises(ConstraintError, match="cannot be NULL"):
            e2.execute("AAYUSH INSERT INTO accounts (id, email) VALUES (3, NULL)")

        # Type mismatch rejected after reload
        with pytest.raises(ConstraintError, match="expects INT"):
            e2.execute("AAYUSH INSERT INTO accounts (id, email) VALUES ('not_int', 'c@example.com')")


def test_missing_database_file_starts_empty_and_publishes_a_v4_manifest(tmp_path: Path):
    db_file = tmp_path / "nested" / "new.aaydb"
    assert not db_file.exists()

    e = Engine(db_file)
    # Format v4 persists its root manifest (database identity) when it opens.
    assert db_file.exists()
    assert e.db.tables == {}
    with pytest.raises(ExecutionError, match="does not exist"):
        e.execute("AAYUSH SELECT * FROM users")

    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY)")
    assert db_file.exists()
    e.close()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   \n",
        "{not valid json",
        "[]",
        json.dumps({"format": "other", "format_version": 1, "tables": {}}),
        json.dumps({"format": "aaysql", "format_version": 99, "tables": {}}),
        json.dumps({"format": "aaysql", "format_version": 1, "tables": []}),
        json.dumps({"format": "aaysql", "format_version": 1, "tables": {"u": {"name": "wrong", "columns": [], "rows": []}}}),
        json.dumps(
            {
                "format": "aaysql",
                "format_version": 1,
                "tables": {
                    "u": {
                        "name": "u",
                        "columns": [
                            {
                                "name": "id",
                                "type_name": "INT",
                                "primary_key": True,
                                "not_null": False,
                                "unique": False,
                                "default": None,
                                "has_default": False,
                            }
                        ],
                        "rows": [{"id": "bad_int"}],
                    }
                },
            }
        ),
        json.dumps(
            {
                "format": "aaysql",
                "format_version": 1,
                "tables": {
                    "u": {
                        "name": "u",
                        "columns": [
                            {
                                "name": "id",
                                "type_name": "INT",
                                "primary_key": True,
                                "not_null": False,
                                "unique": False,
                                "default": None,
                                "has_default": False,
                            }
                        ],
                        "rows": [{"id": 1}, {"id": 1}],
                    }
                },
            }
        ),
    ],
)
def test_malformed_database_files_raise_storage_error(tmp_path: Path, content: str):
    db_file = tmp_path / "bad.aaydb"
    db_file.write_text(content, encoding="utf-8")
    with pytest.raises(StorageError):
        Engine(db_file)
    # Original corrupted file must not be overwritten or discarded.
    assert db_file.read_text(encoding="utf-8") == content


def test_directory_as_db_path_raises_storage_error(tmp_path: Path):
    with pytest.raises(StorageError, match="directory"):
        Engine(tmp_path)


def test_failed_mutations_do_not_corrupt_persisted_data(tmp_path: Path):
    db_file = tmp_path / "safe.aaydb"
    with Engine(db_file) as e1:
        e1.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL)")
        e1.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush'), (2, 'Alex')")

    baseline = db_file.read_text(encoding="utf-8")

    with Engine(db_file) as e2:
        # Missing AAYUSH
        with pytest.raises(ParserError):
            e2.execute("INSERT INTO users VALUES (3, 'Other')")
        # Duplicate AAYUSH
        with pytest.raises(ParserError, match="Duplicate AAYUSH"):
            e2.execute("AAYUSH AAYUSH DELETE FROM users")
        # Multi-row INSERT partial failure must be atomic (row 3 must NOT remain!)
        with pytest.raises(ConstraintError):
            e2.execute("AAYUSH INSERT INTO users VALUES (3, 'Valid'), (3, 'Collision')")
        # Multi-row UPDATE colliding PK must be atomic
        with pytest.raises(ConstraintError):
            e2.execute("AAYUSH UPDATE users SET id = 99")
        # NULL constraint failure
        with pytest.raises(ConstraintError):
            e2.execute("AAYUSH UPDATE users SET name = NULL WHERE id = 1")
        # Drop nonexistent table
        with pytest.raises(ExecutionError):
            e2.execute("AAYUSH DROP TABLE missing")

        assert db_file.read_text(encoding="utf-8") == baseline

    with Engine(db_file) as e3:
        assert e3.execute("AAYUSH SELECT * FROM users ORDER BY id ASC")["rows"] == [
            {"id": 1, "name": "Aayush"},
            {"id": 2, "name": "Alex"},
        ]


def test_select_does_not_rewrite_database_file(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "readonly.aaydb"
    with Engine(db_file) as e:
        e.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        e.execute("AAYUSH INSERT INTO t VALUES (1, 'one')")

    with pytest.raises(StorageError, match="closed"):
        e.execute("AAYUSH SELECT * FROM t")

    with Engine(db_file) as e2:
        def fail_save(_db):
            raise AssertionError("save() must not be called on SELECT")

        monkeypatch.setattr(e2.storage, "save", fail_save)
        res = e2.execute("AAYUSH SELECT * FROM t")
        assert res["rows"] == [{"id": 1, "v": "one"}]


def test_save_failure_rolls_back_in_memory_state(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "rollback.aaydb"
    e = Engine(db_file)
    e.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")

    def broken_save(_db, _changes=None):
        raise StorageError("disk full")

    monkeypatch.setattr(e.storage, "save", broken_save)
    with pytest.raises(StorageError, match="disk full"):
        e.execute("AAYUSH INSERT INTO t VALUES (1)")

    # In-memory state stays consistent with disk.
    assert e.execute("AAYUSH SELECT * FROM t")["rows"] == []


def test_no_temp_files_left_behind(tmp_path: Path):
    db_file = tmp_path / "clean.aaydb"
    with Engine(db_file) as e:
        e.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        e.execute("AAYUSH INSERT INTO t VALUES (1)")
    files = sorted(p.name for p in tmp_path.iterdir())
    # The lease file persists by contract (its existence grants nothing; the OS
    # lock does), but no temporary artifacts may remain.
    assert files == [
        "clean.aaydb",
        "clean.aaydb.seg",
        "clean.aaydb.server.lock",
    ]


def test_v01_query_features_and_null_ordering(tmp_path: Path):
    db_file = tmp_path / "features.aaydb"
    with Engine(db_file) as e:
        e.execute("AAYUSH CREATE TABLE scores (id INT PRIMARY KEY, team TEXT, pts INT)")
        e.execute(
            """
            AAYUSH INSERT INTO scores VALUES
                (1, 'A', 10),
                (2, 'B', NULL),
                (3, 'A', 30),
                (4, 'C', NULL),
                (5, 'B', 20)
            """
        )

    with Engine(db_file) as e2:
        r1 = e2.execute(
            "AAYUSH SELECT team AS grp, pts FROM scores WHERE (team = 'A' OR pts <> 20) AND NOT pts < 15 ORDER BY pts DESC LIMIT 2"
        )
        assert r1["columns"] == ["grp", "pts"]
        assert r1["rows"] == [{"grp": "A", "pts": 30}]

        # Multiple NULLs in ORDER BY should not crash.
        r2 = e2.execute("AAYUSH SELECT id, pts FROM scores ORDER BY pts ASC, id ASC")
        assert [row["id"] for row in r2["rows"]] == [1, 5, 3, 2, 4]


def test_in_memory_mode_does_not_persist():
    e1 = Engine(":memory:")
    e1.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
    e1.execute("AAYUSH INSERT INTO t VALUES (1)")
    assert not e1.is_persistent

    e2 = Engine()
    assert not e2.is_persistent
    with pytest.raises(ExecutionError, match="does not exist"):
        e2.execute("AAYUSH SELECT * FROM t")
