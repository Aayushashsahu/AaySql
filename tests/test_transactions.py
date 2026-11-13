import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql import Engine
from aaysql.errors import (
    ConstraintError,
    DatabaseLockedError,
    ExecutionError,
    ParserError,
    StorageError,
)


def _create(db_path: Path | None = None) -> Engine:
    engine = Engine(db_path)
    engine.execute(
        "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT NOT NULL, balance INT)"
    )
    engine.execute(
        "AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000), (2, 'Alex', 500)"
    )
    return engine


def _balances(engine: Engine) -> list[dict]:
    return engine.execute("AAYUSH SELECT id, balance FROM accounts ORDER BY id ASC")["rows"]


def _run_cli(args: list[str], cwd: Path, stdin_text: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    src_dir = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "aaysql.cli", *args],
        cwd=cwd,
        input=stdin_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


# --- basic lifecycle ---


def test_begin_commit_persists_every_change(tmp_path: Path):
    db_file = tmp_path / "commit.aaydb"
    engine = _create(db_file)

    assert engine.execute("AAYUSH BEGIN")["message"] == "Transaction started"
    assert engine.in_transaction
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.execute("AAYUSH UPDATE accounts SET balance = 900 WHERE id = 1")
    assert engine.execute("AAYUSH COMMIT")["message"] == "Transaction committed"
    assert not engine.in_transaction
    engine.close()

    with Engine(db_file) as reopened:
        assert _balances(reopened) == [
            {"id": 1, "balance": 900},
            {"id": 2, "balance": 500},
            {"id": 3, "balance": 250},
        ]


def test_begin_rollback_discards_every_change(tmp_path: Path):
    db_file = tmp_path / "rollback.aaydb"
    engine = _create(db_file)
    before = db_file.read_bytes()

    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH DELETE FROM accounts")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (9, 'Ghost', 1)")
    assert engine.execute("AAYUSH ROLLBACK")["message"] == "Transaction rolled back"
    assert not engine.in_transaction

    assert _balances(engine) == [
        {"id": 1, "balance": 1000},
        {"id": 2, "balance": 500},
    ]
    # Nothing was written while the transaction was open.
    assert db_file.read_bytes() == before
    engine.close()


def test_multiple_inserts_and_updates_inside_one_transaction(tmp_path: Path):
    db_file = tmp_path / "multi.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Neha', 400)")
    engine.execute("AAYUSH UPDATE accounts SET balance = balance WHERE id = 2")
    engine.execute("AAYUSH DELETE FROM accounts WHERE id = 1")
    engine.execute("AAYUSH COMMIT")
    engine.close()

    with Engine(db_file) as reopened:
        assert _balances(reopened) == [
            {"id": 2, "balance": 500},
            {"id": 3, "balance": 250},
            {"id": 4, "balance": 400},
        ]


def test_ddl_and_index_ddl_roll_back(tmp_path: Path):
    db_file = tmp_path / "ddl.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH CREATE TABLE temp (id INT PRIMARY KEY)")
    engine.execute("AAYUSH CREATE INDEX idx_accounts_balance ON accounts (balance)")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.execute("AAYUSH DROP TABLE accounts")
    engine.execute("AAYUSH ROLLBACK")

    assert "temp" not in engine.db.tables
    assert "idx_accounts_balance" not in engine.db.indexes
    assert _balances(engine) == [
        {"id": 1, "balance": 1000},
        {"id": 2, "balance": 500},
    ]
    assert engine.execute("AAYUSH EXPLAIN SELECT * FROM accounts WHERE balance = 500")["message"].count(
        "TABLE SCAN"
    ) == 1
    engine.close()


def test_index_changes_commit_and_survive_reopen(tmp_path: Path):
    db_file = tmp_path / "index_tx.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH CREATE INDEX idx_accounts_balance ON accounts (balance)")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    plan = engine.execute("AAYUSH EXPLAIN SELECT * FROM accounts WHERE balance = 250")["message"]
    assert "INDEX SCAN: idx_accounts_balance" in plan
    engine.execute("AAYUSH COMMIT")
    engine.close()

    with Engine(db_file) as reopened:
        assert "idx_accounts_balance" in reopened.db.indexes
        assert reopened.execute("AAYUSH SELECT id FROM accounts WHERE balance = 250")["rows"] == [
            {"id": 3}
        ]
        reopened.db.indexes["idx_accounts_balance"].validate()


# --- failure handling ---


def test_failed_statement_inside_transaction_leaves_no_partial_change(tmp_path: Path):
    db_file = tmp_path / "constraint.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")

    with pytest.raises(ConstraintError):
        engine.execute("AAYUSH INSERT INTO accounts VALUES (4, 'Neha', 1), (3, 'Dup', 2)")

    # The valid first row of the failed batch must not survive, and the earlier
    # transaction statement must still be there.
    assert _balances(engine) == [
        {"id": 1, "balance": 1000},
        {"id": 2, "balance": 500},
        {"id": 3, "balance": 250},
    ]

    with pytest.raises(ConstraintError):
        engine.execute("AAYUSH UPDATE accounts SET id = 1")
    assert len(_balances(engine)) == 3

    engine.execute("AAYUSH COMMIT")
    engine.close()
    with Engine(db_file) as reopened:
        assert len(_balances(reopened)) == 3


def test_commit_persistence_failure_rolls_back_and_reports(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "failed_commit.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")

    def broken_save(_db, _changes=None):
        raise StorageError("disk full")

    monkeypatch.setattr(engine.storage, "save", broken_save)
    with pytest.raises(StorageError, match="disk full"):
        engine.execute("AAYUSH COMMIT")

    assert not engine.in_transaction
    # Memory matches the file, which was never replaced.
    assert _balances(engine) == [
        {"id": 1, "balance": 1000},
        {"id": 2, "balance": 500},
    ]
    engine.close()

    with Engine(db_file) as reopened:
        assert len(_balances(reopened)) == 2


def test_nested_begin_and_missing_transaction_errors(tmp_path: Path):
    engine = _create(tmp_path / "errors.aaydb")
    with pytest.raises(ExecutionError, match="COMMIT without an active transaction"):
        engine.execute("AAYUSH COMMIT")
    with pytest.raises(ExecutionError, match="ROLLBACK without an active transaction"):
        engine.execute("AAYUSH ROLLBACK")

    engine.execute("AAYUSH BEGIN")
    with pytest.raises(ExecutionError, match="Nested transactions are not supported"):
        engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH ROLLBACK")

    with pytest.raises(ParserError, match="Duplicate AAYUSH"):
        engine.execute("AAYUSH AAYUSH BEGIN")
    with pytest.raises(ParserError):
        engine.execute("AAYUSH BEGIN EXTRA")
    engine.close()


# --- visibility and isolation ---


def test_select_visibility_inside_and_outside_transaction(tmp_path: Path):
    db_file = tmp_path / "visibility.aaydb"
    engine = _create(db_file)

    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.execute("AAYUSH UPDATE accounts SET balance = 42 WHERE id = 1")
    # Uncommitted changes are visible to the active transaction.
    assert _balances(engine) == [
        {"id": 1, "balance": 42},
        {"id": 2, "balance": 500},
        {"id": 3, "balance": 250},
    ]

    # A second engine may not open the file while this one owns it: opening can
    # recover the WAL, and this transaction is still in memory.
    with pytest.raises(DatabaseLockedError):
        Engine(db_file)

    engine.execute("AAYUSH COMMIT")
    engine.close()
    # A separate owner now sees only the committed file.
    with Engine(db_file) as reader:
        assert len(_balances(reader)) == 3


def test_close_with_active_transaction_rolls_back(tmp_path: Path):
    db_file = tmp_path / "close.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.close()

    with Engine(db_file) as reopened:
        assert len(_balances(reopened)) == 2


def test_autocommit_outside_transactions_is_unchanged(tmp_path: Path):
    db_file = tmp_path / "autocommit.aaydb"
    engine = _create(db_file)
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")

    with pytest.raises(DatabaseLockedError):
        Engine(db_file)
    engine.close()

    with Engine(db_file) as other:
        assert len(_balances(other)) == 3


def test_in_memory_transactions_work():
    engine = _create()
    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.execute("AAYUSH ROLLBACK")
    assert len(_balances(engine)) == 2

    engine.execute("AAYUSH BEGIN")
    engine.execute("AAYUSH INSERT INTO accounts VALUES (3, 'Mika', 250)")
    engine.execute("AAYUSH COMMIT")
    assert len(_balances(engine)) == 3
    engine.close()


# --- CLI behavior ---


def test_cli_exit_with_open_transaction_rolls_back_and_warns(tmp_path: Path):
    db_file = tmp_path / "cli.aaydb"

    session1 = (
        "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT NOT NULL, balance INT);\n"
        "AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000);\n"
        "exit\n"
    )
    first = _run_cli(["--db", str(db_file)], cwd=tmp_path, stdin_text=session1)
    assert first.returncode == 0, first.stderr

    session2 = (
        "AAYUSH BEGIN;\n"
        "AAYUSH INSERT INTO accounts VALUES (2, 'Uncommitted', 500);\n"
        "exit\n"
    )
    second = _run_cli(["--db", str(db_file)], cwd=tmp_path, stdin_text=session2)
    assert second.returncode == 0, second.stderr
    assert "WARNING: rolling back uncommitted transaction on exit" in second.stderr

    verify = _run_cli(
        ["--db", str(db_file)],
        cwd=tmp_path,
        stdin_text="AAYUSH SELECT id FROM accounts ORDER BY id ASC;\nexit\n",
    )
    assert verify.returncode == 0, verify.stderr
    assert "(1 row)" in verify.stdout
    assert "Uncommitted" not in verify.stdout


def test_cli_script_with_commit_persists(tmp_path: Path):
    db_file = tmp_path / "script.aaydb"
    script = tmp_path / "tx.aay"
    script.write_text(
        "AAYUSH CREATE TABLE accounts (id INT PRIMARY KEY, owner TEXT NOT NULL, balance INT);\n"
        "AAYUSH BEGIN;\n"
        "AAYUSH INSERT INTO accounts VALUES (1, 'Aayush', 1000);\n"
        "AAYUSH INSERT INTO accounts VALUES (2, 'Alex', 500);\n"
        "AAYUSH COMMIT;\n",
        encoding="utf-8",
    )
    result = _run_cli(["--db", str(db_file), str(script)], cwd=tmp_path, stdin_text="")
    assert result.returncode == 0, result.stderr
    assert "WARNING" not in result.stderr

    verify = _run_cli(
        ["--db", str(db_file)],
        cwd=tmp_path,
        stdin_text="AAYUSH SELECT id FROM accounts;\nexit\n",
    )
    assert verify.returncode == 0
    assert "(2 rows)" in verify.stdout
