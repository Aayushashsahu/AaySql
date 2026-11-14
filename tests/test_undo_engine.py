"""Engine integration: undo-journal transactions, statement atomicity, and no-clone proof."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.errors import (
    AmbiguousCommitError,
    ConstraintError,
    ExecutionError,
    StorageError,
)
from aaysql.index import Index
from aaysql.storage import ENGINE_VERSION, FORMAT_VERSION, Database
from aaysql.undo import UpdateUndo


def _seed_legacy_database(db_file: Path) -> None:
    if db_file.exists():
        return
    db_file.write_text(
        json.dumps(
            {
                "format": "aaysql",
                "format_version": FORMAT_VERSION,
                "engine_version": ENGINE_VERSION,
                "wal_lsn": 0,
                "tables": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )


def _engine(db_path: Path | None = None) -> Engine:
    engine = Engine(db_path)
    engine.execute(
        "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT NOT NULL, balance INT)"
    )
    engine.execute("AAYUSH CREATE INDEX idx_accounts_balance ON accounts (balance)")
    engine.execute(
        "AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000), (2, 'Alex', 500)"
    )
    return engine


def _balances(engine: Engine) -> list[dict]:
    return engine.execute("AAYUSH SELECT id, balance FROM accounts ORDER BY id ASC")["rows"]


def _forbid_clone(monkeypatch) -> None:
    def fail(_self):
        raise AssertionError("Database.clone() must not run on the normal execution path")

    monkeypatch.setattr(Database, "clone", fail)


# --- journal and transaction lifecycle ------------------------------------


def test_begin_creates_a_marker_not_a_database_clone(tmp_path: Path, monkeypatch):
    engine = _engine(tmp_path / "begin.aaydb")
    try:
        table = engine.db.table("accounts")
        revision = table.revision
        _forbid_clone(monkeypatch)
        engine.execute("AAYUSH BEGIN")
        assert engine.in_transaction
        assert engine.undo_entries == 0
        assert engine.db.table("accounts") is table
        assert table.revision == revision
        engine.execute("AAYUSH ROLLBACK")
        assert engine.db.table("accounts") is table
    finally:
        engine.close()


def test_rollback_restores_rows_indexes_and_statistics(tmp_path: Path):
    engine = _engine(tmp_path / "rollback.aaydb")
    try:
        base = _balances(engine)
        before_revision = engine.db.table("accounts").revision
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        engine.execute("AAYUSH UPDATE accounts SET balance = 42 WHERE id = 1")
        engine.execute("AAYUSH DELETE FROM accounts WHERE id = 2")
        assert _balances(engine) == [
            {"id": 1, "balance": 42},
            {"id": 3, "balance": 250},
        ]
        engine.execute("AAYUSH ROLLBACK")
        assert _balances(engine) == base
        assert engine.undo_entries == 0
        table = engine.db.table("accounts")
        assert table.revision != before_revision
        stats = engine.db.stats.get(table)
        assert stats.revision == table.revision
        assert stats.row_count == 2
        index = engine.db.indexes["idx_accounts_balance"]
        index.validate()
        assert len(index) == 2
        assert [row["id"] for row in index.lookup(500)] == [2]
        assert index.lookup(250) == []
        assert engine.execute(
            "AAYUSH SELECT id FROM accounts WHERE balance = 500"
        )["rows"] == [{"id": 2}]
    finally:
        engine.close()


def test_statement_atomicity_keeps_earlier_statements_and_allows_commit(tmp_path: Path):
    engine = _engine(tmp_path / "statement.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        with pytest.raises(ConstraintError):
            engine.execute(
                "AAYUSH INSERT INTO accounts VALUES "
                "(4, 'Neha', 1), (3, 'Duplicate', 2), (5, 'Later', 3)"
            )
        # The failed statement left no rows or index entries behind.
        assert [row["id"] for row in _balances(engine)] == [1, 2, 3]
        assert engine.db.indexes["idx_accounts_balance"].lookup(1) == []
        assert engine.undo_entries == 1  # only the first successful INSERT
        engine.execute("AAYUSH INSERT INTO accounts VALUES (6, 'B', 6)")
        engine.execute("AAYUSH COMMIT")
        assert engine.undo_entries == 0
        assert [row["id"] for row in _balances(engine)] == [1, 2, 3, 6]
    finally:
        engine.close()


def test_commit_with_no_mutations_and_repeated_lifecycle_errors(tmp_path: Path):
    engine = _engine(tmp_path / "empty.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH COMMIT")
        assert engine.undo_entries == 0
        with pytest.raises(ExecutionError, match="COMMIT without an active transaction"):
            engine.execute("AAYUSH COMMIT")
        with pytest.raises(ExecutionError, match="ROLLBACK without an active transaction"):
            engine.execute("AAYUSH ROLLBACK")
        engine.execute("AAYUSH BEGIN")
        with pytest.raises(ExecutionError, match="Nested transactions"):
            engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH ROLLBACK")
        engine.close()
        engine.close()
        with pytest.raises(StorageError, match="closed Engine"):
            engine.execute("AAYUSH SELECT * FROM accounts")
    finally:
        engine.close()


def test_close_with_active_transaction_applies_the_journal(tmp_path: Path):
    engine = _engine(tmp_path / "close.aaydb")
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.close()
    with Engine(tmp_path / "close.aaydb") as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2]


# --- injected partial failures --------------------------------------------


def test_failed_insert_after_partial_index_work_is_fully_undone(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "partial_insert.aaydb")
    try:
        original = Index.add
        calls = {"count": 0}

        def flaky(self, row):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected index failure")
            return original(self, row)

        monkeypatch.setattr(Index, "add", flaky)
        engine.execute("AAYUSH BEGIN")
        with pytest.raises(RuntimeError, match="injected index failure"):
            engine.execute(
                "AAYUSH INSERT INTO accounts VALUES (3, 'A', 3), (4, 'B', 4)"
            )
        monkeypatch.setattr(Index, "add", original)
        assert [row["id"] for row in _balances(engine)] == [1, 2]
        index = engine.db.indexes["idx_accounts_balance"]
        assert len(index) == 2
        index.validate()
        assert engine.undo_entries == 0
        engine.execute("AAYUSH INSERT INTO accounts VALUES (5, 'C', 5)")
        engine.execute("AAYUSH COMMIT")
        assert [row["id"] for row in _balances(engine)] == [1, 2, 5]
    finally:
        engine.close()


def test_failed_update_during_index_update_restores_rows(tmp_path: Path, monkeypatch):
    engine = _engine(tmp_path / "partial_update.aaydb")
    try:
        original = Index.add

        def flaky(self, row):
            raise RuntimeError("injected update index failure")

        monkeypatch.setattr(Index, "add", flaky)
        engine.execute("AAYUSH BEGIN")
        with pytest.raises(RuntimeError, match="injected update index failure"):
            engine.execute("AAYUSH UPDATE accounts SET balance = balance + 1")
        monkeypatch.setattr(Index, "add", original)
        assert _balances(engine) == [
            {"id": 1, "balance": 1000},
            {"id": 2, "balance": 500},
        ]
        index = engine.db.indexes["idx_accounts_balance"]
        index.validate()
        assert [row["id"] for row in index.lookup(1000)] == [1]
        assert index.lookup(1001) == []
        assert engine.undo_entries == 0
        engine.execute("AAYUSH ROLLBACK")
        assert _balances(engine) == [
            {"id": 1, "balance": 1000},
            {"id": 2, "balance": 500},
        ]
    finally:
        engine.close()


def test_failed_delete_during_index_maintenance_restores_positions(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "partial_delete.aaydb")
    try:
        original = Index.remove

        def flaky(self, row, **kwargs):
            raise RuntimeError("injected delete index failure")

        monkeypatch.setattr(Index, "remove", flaky)
        engine.execute("AAYUSH BEGIN")
        with pytest.raises(RuntimeError, match="injected delete index failure"):
            engine.execute("AAYUSH DELETE FROM accounts WHERE id <= 2")
        monkeypatch.setattr(Index, "remove", original)
        assert _balances(engine) == [
            {"id": 1, "balance": 1000},
            {"id": 2, "balance": 500},
        ]
        index = engine.db.indexes["idx_accounts_balance"]
        index.validate()
        assert len(index) == 2
        assert [row["id"] for row in index.lookup(500)] == [2]
        assert engine.undo_entries == 0
        engine.execute("AAYUSH ROLLBACK")
    finally:
        engine.close()


def test_failure_during_drop_table_restores_table_and_indexes(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "partial_drop.aaydb")
    try:
        engine.execute("AAYUSH CREATE INDEX idx_accounts_owner ON accounts (owner)")
        original = Database.drop_table

        def flaky(self, name):
            original(self, name)
            raise RuntimeError("injected drop failure")

        monkeypatch.setattr(Database, "drop_table", flaky)
        engine.execute("AAYUSH BEGIN")
        with pytest.raises(RuntimeError, match="injected drop failure"):
            engine.execute("AAYUSH DROP TABLE accounts")
        monkeypatch.setattr(Database, "drop_table", original)
        assert [row["id"] for row in _balances(engine)] == [1, 2]
        assert set(engine.db.indexes) == {
            "idx_accounts_balance",
            "idx_accounts_owner",
        }
        for index in engine.db.indexes.values():
            index.validate()
        assert engine.undo_entries == 0
        engine.execute("AAYUSH ROLLBACK")
    finally:
        engine.close()


def test_journal_memory_is_per_mutation_not_per_database(tmp_path: Path):
    engine = _engine(tmp_path / "memory.aaydb")
    try:
        engine.execute(
            "AAYUSH CREATE TABLE big (id INT PRIMARY KEY, value INT, note TEXT)"
        )
        engine.execute(
            "AAYUSH INSERT INTO big VALUES "
            + ", ".join(
                f"({i}, {i}, 'payload-{i}')" for i in range(1, 2001)
            )
        )
        engine.execute("AAYUSH BEGIN")
        for row_id in range(1, 6):
            engine.execute(
                f"AAYUSH UPDATE big SET note = 'changed-{row_id}' WHERE id = {row_id}"
            )
        assert engine.undo_entries == 5
        for entry in engine._journal._entries:
            assert isinstance(entry, UpdateUndo)
            assert set(entry.before) == {"id", "value", "note"}
        engine.execute("AAYUSH ROLLBACK")
        assert engine.undo_entries == 0
    finally:
        engine.close()


def test_failure_after_create_table_rolls_the_table_back(tmp_path: Path, monkeypatch):
    engine = _engine(tmp_path / "partial_ddl.aaydb")
    try:
        def broken(_table):
            raise ExecutionError("injected recorder failure")

        monkeypatch.setattr(engine._recorder, "record_create_table", broken)
        engine.execute("AAYUSH BEGIN")
        with pytest.raises(ExecutionError, match="injected recorder"):
            engine.execute("AAYUSH CREATE TABLE scratch (id INT PRIMARY KEY)")
        assert "scratch" not in engine.db.tables
        assert engine.undo_entries == 0
        monkeypatch.undo()
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        engine.execute("AAYUSH COMMIT")
        assert [row["id"] for row in _balances(engine)] == [1, 2, 3]
    finally:
        engine.close()


# --- DDL inverses through SQL --------------------------------------------


def test_ddl_rollback_restores_catalog_and_indexes(tmp_path: Path):
    engine = _engine(tmp_path / "ddl.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH CREATE TABLE temp (id INT PRIMARY KEY)")
        engine.execute("AAYUSH CREATE INDEX idx_accounts_owner ON accounts (owner)")
        engine.execute("AAYUSH DROP INDEX idx_accounts_balance")
        engine.execute("AAYUSH DROP TABLE accounts")
        assert "accounts" not in engine.db.tables
        assert "idx_accounts_owner" not in engine.db.indexes
        engine.execute("AAYUSH ROLLBACK")
        assert "temp" not in engine.db.tables
        assert set(engine.db.indexes) == {"idx_accounts_balance"}
        assert [row["id"] for row in _balances(engine)] == [1, 2]
        engine.db.indexes["idx_accounts_balance"].validate()
        assert engine.execute(
            "AAYUSH EXPLAIN SELECT * FROM accounts WHERE balance = 500"
        )["message"].count("INDEX SCAN: idx_accounts_balance") == 1
    finally:
        engine.close()


def test_drop_table_then_recreate_same_name_then_rollback(tmp_path: Path):
    engine = _engine(tmp_path / "recreate_table.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH DROP TABLE accounts")
        engine.execute(
            "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT, balance INT)"
        )
        engine.execute("AAYUSH INSERT INTO accounts VALUES (9, 'New', 9)")
        engine.execute("AAYUSH ROLLBACK")
        assert [row["id"] for row in _balances(engine)] == [1, 2]
        assert set(engine.db.indexes) == {"idx_accounts_balance"}
        assert engine.db.indexes["idx_accounts_balance"].lookup(1000)[0]["id"] == 1
    finally:
        engine.close()


def test_create_then_drop_same_object_then_rollback(tmp_path: Path):
    engine = _engine(tmp_path / "ddl_sequence.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH CREATE TABLE temp (id INT PRIMARY KEY)")
        engine.execute("AAYUSH CREATE INDEX idx_temp_id ON temp (id)")
        engine.execute("AAYUSH DROP INDEX idx_temp_id")
        engine.execute("AAYUSH DROP TABLE temp")
        engine.execute("AAYUSH CREATE INDEX idx_accounts_owner ON accounts (owner)")
        engine.execute("AAYUSH DROP TABLE accounts")
        engine.execute("AAYUSH ROLLBACK")
        assert "temp" not in engine.db.tables
        assert set(engine.db.indexes) == {"idx_accounts_balance"}
        assert [row["id"] for row in _balances(engine)] == [1, 2]
    finally:
        engine.close()


def test_repeated_updates_to_one_row_restore_the_original(tmp_path: Path):
    engine = _engine(tmp_path / "repeat_update.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH UPDATE accounts SET balance = 1 WHERE id = 1")
        engine.execute("AAYUSH UPDATE accounts SET balance = 2 WHERE id = 1")
        engine.execute("AAYUSH UPDATE accounts SET owner = 'Changed' WHERE id = 1")
        engine.execute("AAYUSH ROLLBACK")
        assert engine.execute(
            "AAYUSH SELECT owner, balance FROM accounts WHERE id = 1"
        )["rows"] == [{"owner": "Aayush", "balance": 1000}]
        engine.db.indexes["idx_accounts_balance"].validate()
        assert engine.db.indexes["idx_accounts_balance"].lookup(1000)[0]["id"] == 1
    finally:
        engine.close()


def test_drop_index_then_recreate_same_name_then_rollback(tmp_path: Path):
    engine = _engine(tmp_path / "recreate_index.aaydb")
    try:
        original = engine.db.indexes["idx_accounts_balance"]
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH DROP INDEX idx_accounts_balance")
        engine.execute("AAYUSH CREATE INDEX idx_accounts_balance ON accounts (owner)")
        engine.execute("AAYUSH ROLLBACK")
        restored = engine.db.indexes["idx_accounts_balance"]
        assert restored is original
        assert restored.column == "balance"
        assert restored.lookup(500)[0]["id"] == 2
        assert restored.lookup(1000)[0]["id"] == 1
        assert restored.lookup(250) == []
    finally:
        engine.close()


def test_duplicate_rows_keep_exact_order_through_rollback(tmp_path: Path):
    engine = Engine(tmp_path / "dups.aaydb")
    try:
        engine.execute("AAYUSH CREATE TABLE dups (name TEXT, value INT)")
        engine.execute("AAYUSH CREATE INDEX idx_dups_value ON dups (value)")
        engine.execute(
            "AAYUSH INSERT INTO dups VALUES ('a', 1), ('b', 1), ('c', 2), ('d', 1)"
        )
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH DELETE FROM dups WHERE value = 1")
        engine.execute("AAYUSH INSERT INTO dups VALUES ('z', 1)")
        engine.execute("AAYUSH ROLLBACK")
        assert engine.execute(
            "AAYUSH SELECT name FROM dups WHERE value = 1"
        )["rows"] == [{"name": "a"}, {"name": "b"}, {"name": "d"}]
        index = engine.db.indexes["idx_dups_value"]
        index.validate()
        assert [row["name"] for row in index.lookup(1)] == ["a", "b", "d"]
    finally:
        engine.close()


def test_forward_wal_contains_only_surviving_changes(tmp_path: Path):
    db_path = tmp_path / "forward.aaydb"
    engine = _engine(db_path)
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        with pytest.raises(ConstraintError):
            engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Ghost', 1), (1, 'Dup', 2)")
        engine.execute("AAYUSH COMMIT")
        wal_text = Path(f"{db_path}.wal").read_text(encoding="utf-8")
        assert "Ghost" not in wal_text
        assert "Mika" in wal_text
    finally:
        engine.close()
    with Engine(db_path) as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2, 3]


# --- autocommit and commit classification ---------------------------------


def test_autocommit_success_discards_undo_entries(tmp_path: Path):
    engine = _engine(tmp_path / "auto_ok.aaydb")
    try:
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        assert engine.undo_entries == 0
        engine.execute("AAYUSH UPDATE accounts SET balance = 7 WHERE id = 3")
        assert engine.undo_entries == 0
        engine.execute("AAYUSH DELETE FROM accounts WHERE id = 3")
        assert engine.undo_entries == 0
        assert [row["id"] for row in _balances(engine)] == [1, 2]
    finally:
        engine.close()


def test_in_memory_autocommit_failure_is_statement_atomic(monkeypatch):
    engine = Engine()
    try:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, value INT)")
        engine.execute("AAYUSH CREATE INDEX idx_t_value ON t (value)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1, 1), (2, 2)")
        original = Index.add

        def flaky(self, row):
            raise RuntimeError("injected memory failure")

        monkeypatch.setattr(Index, "add", flaky)
        with pytest.raises(RuntimeError, match="injected memory failure"):
            engine.execute("AAYUSH INSERT INTO t VALUES (3, 3)")
        monkeypatch.setattr(Index, "add", original)
        assert engine.execute("AAYUSH SELECT id FROM t ORDER BY id")["rows"] == [
            {"id": 1},
            {"id": 2},
        ]
        assert len(engine.db.indexes["idx_t_value"]) == 2
        assert engine.undo_entries == 0
        engine.execute("AAYUSH INSERT INTO t VALUES (3, 3)")
        assert engine.execute("AAYUSH SELECT COUNT(*) AS c FROM t")["rows"] == [{"c": 3}]
    finally:
        engine.close()


def test_persistent_autocommit_failure_rolls_back_and_allows_more_work(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "auto_fail.aaydb")
    try:
        def broken(_db, _changes=None):
            raise StorageError("disk full")

        monkeypatch.setattr(engine.storage, "save", broken)
        with pytest.raises(StorageError, match="disk full"):
            engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        assert [row["id"] for row in _balances(engine)] == [1, 2]
        assert engine.undo_entries == 0
        monkeypatch.undo()
        engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Back', 400)")
        assert [row["id"] for row in _balances(engine)] == [1, 2, 4]
    finally:
        engine.close()
    with Engine(tmp_path / "auto_fail.aaydb") as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2, 4]


@pytest.mark.parametrize(
    "statement",
    [
        "AAYUSH UPDATE accounts SET balance = 1 WHERE id = 1",
        "AAYUSH DELETE FROM accounts WHERE id = 1",
        "AAYUSH CREATE INDEX idx_owner ON accounts (owner)",
        "AAYUSH DROP INDEX idx_accounts_balance",
        "AAYUSH DROP TABLE accounts",
    ],
)
def test_persistent_autocommit_failure_restores_each_mutation_kind(
    tmp_path: Path, monkeypatch, statement: str
):
    engine = _engine(tmp_path / "auto_kinds.aaydb")
    try:
        def broken(_db, _changes=None):
            raise StorageError("disk full")

        monkeypatch.setattr(engine.storage, "save", broken)
        with pytest.raises(StorageError, match="disk full"):
            engine.execute(statement)
        assert _balances(engine) == [
            {"id": 1, "balance": 1000},
            {"id": 2, "balance": 500},
        ]
        assert set(engine.db.indexes) == {"idx_accounts_balance"}
        assert engine.undo_entries == 0
        engine.db.indexes["idx_accounts_balance"].validate()
    finally:
        engine.close()


def test_commit_persistence_failure_undoes_the_whole_transaction(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "commit_fail.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        engine.execute("AAYUSH UPDATE accounts SET balance = 1 WHERE id = 1")

        def broken(_db, _changes=None):
            raise StorageError("disk full")

        monkeypatch.setattr(engine.storage, "save", broken)
        with pytest.raises(StorageError, match="disk full"):
            engine.execute("AAYUSH COMMIT")
        assert not engine.in_transaction
        assert _balances(engine) == [
            {"id": 1, "balance": 1000},
            {"id": 2, "balance": 500},
        ]
        assert engine.undo_entries == 0
        assert engine.db.indexes["idx_accounts_balance"].lookup(1) == []
        monkeypatch.undo()
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (5, 'Again', 5)")
        engine.execute("AAYUSH COMMIT")
    finally:
        engine.close()
    with Engine(tmp_path / "commit_fail.aaydb") as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2, 5]


def test_ambiguous_autocommit_commit_is_not_treated_as_rollback(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "ambiguous_auto.aaydb")
    try:
        def ambiguous(_db, _changes=None):
            raise AmbiguousCommitError("fsync outcome unknown")

        monkeypatch.setattr(engine.storage, "save", ambiguous)
        with pytest.raises(AmbiguousCommitError, match="unknown"):
            engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Maybe', 250)")
        # The row is NOT undone: the commit may be durable. Reads are blocked,
        # so inspect the in-memory table directly.
        assert engine.needs_recovery
        assert [row["id"] for row in engine.db.table("accounts").rows] == [1, 2, 3]
        assert engine.undo_entries == 0
        with pytest.raises(StorageError, match="ambiguous"):
            engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'No', 1)")
    finally:
        engine.close()


def test_ambiguous_transaction_commit_is_not_treated_as_rollback(
    tmp_path: Path, monkeypatch
):
    engine = _engine(tmp_path / "ambiguous_txn.aaydb")
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Maybe', 250)")

        def ambiguous(_db, _changes=None):
            raise AmbiguousCommitError("fsync outcome unknown")

        monkeypatch.setattr(engine.storage, "save", ambiguous)
        with pytest.raises(AmbiguousCommitError):
            engine.execute("AAYUSH COMMIT")
        assert engine.needs_recovery
        assert [row["id"] for row in engine.db.table("accounts").rows] == [1, 2, 3]
        assert engine.undo_entries == 0
    finally:
        engine.close()


def test_durable_commit_with_checkpoint_failure_keeps_the_transaction(
    tmp_path: Path, monkeypatch
):
    db_path = tmp_path / "pending.aaydb"
    engine = _engine(db_path)
    try:
        def broken_checkpoint(_db, _lsn):
            raise StorageError("checkpoint failed")

        monkeypatch.setattr(engine.storage, "_checkpoint", broken_checkpoint)
        # Force the size-triggered checkpoint so the post-commit failure path runs.
        engine.storage.checkpoint_bytes = 1
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Durable', 250)")
        engine.execute("AAYUSH COMMIT")
        # The COMMIT succeeded; only the checkpoint is pending.
        assert engine.undo_entries == 0
        assert engine.storage.pending_checkpoint is True
        assert [row["id"] for row in _balances(engine)] == [1, 2, 3]
    finally:
        engine.close()
    with Engine(db_path) as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2, 3]


# --- legacy backend and no-clone proof -----------------------------------


def test_v3_backend_supports_journal_transactions(tmp_path: Path):
    db_path = tmp_path / "legacy.aaydb"
    _seed_legacy_database(db_path)
    engine = _engine(db_path)
    try:
        assert type(engine.storage).__name__ == "JsonFileStorage"
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        engine.execute("AAYUSH UPDATE accounts SET balance = 1 WHERE id = 1")
        engine.execute("AAYUSH ROLLBACK")
        assert _balances(engine) == [
            {"id": 1, "balance": 1000},
            {"id": 2, "balance": 500},
        ]
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Commit', 400)")
        engine.execute("AAYUSH COMMIT")
        assert engine.undo_entries == 0
    finally:
        engine.close()
    with Engine(db_path) as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2, 4]


def test_no_clone_on_normal_execution_path_v4(tmp_path: Path, monkeypatch):
    engine = _engine(tmp_path / "noclone.aaydb")
    _forbid_clone(monkeypatch)
    try:
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
        engine.execute("AAYUSH UPDATE accounts SET balance = 3 WHERE id = 3")
        engine.execute("AAYUSH DELETE FROM accounts WHERE id = 3")
        engine.execute("AAYUSH CREATE INDEX idx_owner ON accounts (owner)")
        engine.execute("AAYUSH DROP INDEX idx_owner")
        engine.execute("AAYUSH CREATE TABLE scratch (id INT PRIMARY KEY)")
        engine.execute("AAYUSH DROP TABLE scratch")
        with pytest.raises(ConstraintError):
            engine.execute("AAYUSH INSERT INTO accounts VALUES (1, 'Dup', 1)")
        engine.execute("AAYUSH COMMIT")

        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Roll', 4)")
        engine.execute("AAYUSH ROLLBACK")

        engine.execute("AAYUSH INSERT INTO accounts VALUES (5, 'Auto', 5)")

        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO accounts VALUES (6, 'Close', 6)")
        engine.close()
    finally:
        engine.close()
    with Engine(tmp_path / "noclone.aaydb") as reopened:
        assert [row["id"] for row in _balances(reopened)] == [1, 2, 5]


def test_no_clone_on_normal_execution_path_in_memory(monkeypatch):
    engine = Engine()
    try:
        engine.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY, value INT)")
        engine.execute("AAYUSH CREATE INDEX idx_value ON t (value)")
        engine.execute("AAYUSH INSERT INTO t VALUES (1, 1)")
        _forbid_clone(monkeypatch)
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO t VALUES (2, 2)")
        engine.execute("AAYUSH UPDATE t SET value = 3 WHERE id = 2")
        engine.execute("AAYUSH DELETE FROM t WHERE id = 2")
        engine.execute("AAYUSH ROLLBACK")
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO t VALUES (3, 3)")
        engine.execute("AAYUSH COMMIT")
        engine.execute("AAYUSH INSERT INTO t VALUES (4, 4)")
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH INSERT INTO t VALUES (5, 5)")
        engine.close()
        assert engine.undo_entries == 0
    finally:
        engine.close()
