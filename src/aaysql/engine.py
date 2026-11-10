from __future__ import annotations

from pathlib import Path
from typing import Any

from .ast import (
    Begin,
    Commit,
    CreateIndex,
    CreateTable,
    Delete,
    DropIndex,
    DropTable,
    Insert,
    Rollback,
    Update,
)
from .errors import AmbiguousCommitError, StorageError
from .executor import Executor
from .lexer import Lexer
from .parser import Parser
from .storage import Database, JsonFileStorage
from .transaction import TransactionManager

_MUTATION_STATEMENTS = (
    CreateTable,
    DropTable,
    Insert,
    Update,
    Delete,
    CreateIndex,
    DropIndex,
)


class Engine:
    """Top-level AayushSQL engine coordinating lexing, parsing, execution, and persistence.

    Outside an explicit transaction every mutating statement is autocommitted and
    persisted durably through the WAL before checkpointing. Inside ``BEGIN`` ...
    ``COMMIT`` mutations stay in memory until the single commit-time WAL write
    and checkpoint.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.lexer = Lexer()
        if db_path is None or str(db_path) == ":memory:":
            self.db_path: Path | None = None
            self.storage: JsonFileStorage | None = None
            self.db = Database()
        else:
            if isinstance(db_path, str) and not db_path.strip():
                raise StorageError("Database path cannot be empty")
            self.db_path = Path(db_path)
            self.storage = JsonFileStorage(self.db_path)
            self.db = self.storage.load()
        self.executor = Executor(self.db)
        self.transactions = TransactionManager()
        self._closed = False
        self._recovery_required = False

    @property
    def is_persistent(self) -> bool:
        return self.storage is not None

    @property
    def in_transaction(self) -> bool:
        return self.transactions.in_transaction

    @property
    def needs_recovery(self) -> bool:
        return self._recovery_required

    def _ensure_usable(self) -> None:
        if self._closed:
            raise StorageError("Cannot execute statement on a closed Engine")
        if self._recovery_required:
            raise StorageError(
                "Database durability state is ambiguous after a WAL fsync failure; "
                "reopen the database to recover"
            )

    def _install(self, db: Database) -> None:
        """Replace in-memory state and rebind the executor to the new database."""
        self.db = db
        self.executor.db = db

    def execute(self, sql: str) -> dict[str, Any]:
        self._ensure_usable()
        tokens = self.lexer.tokenize(sql)
        stmt = Parser(tokens).parse()

        if isinstance(stmt, Begin):
            return self.begin()
        if isinstance(stmt, Commit):
            return self.commit()
        if isinstance(stmt, Rollback):
            return self.rollback()

        if self.transactions.in_transaction:
            return self._execute_in_transaction(stmt)

        if self.storage is not None and isinstance(stmt, _MUTATION_STATEMENTS):
            snapshot = self.db.clone()
            try:
                result = self.executor.execute(stmt)
                self.storage.save(self.db)
            except AmbiguousCommitError:
                self._recovery_required = True
                raise
            except Exception:
                self._install(snapshot)
                raise
            return result
        return self.executor.execute(stmt)

    def _execute_in_transaction(self, stmt: Any) -> dict[str, Any]:
        if not isinstance(stmt, _MUTATION_STATEMENTS):
            return self.executor.execute(stmt)
        # Each statement stays atomic inside the transaction: a failed statement
        # must not leave partial rows or index entries behind.
        before_statement = self.db.clone()
        try:
            result = self.executor.execute(stmt)
        except Exception:
            self._install(before_statement)
            raise
        self.transactions.record_statement()
        return result

    def begin(self) -> dict[str, Any]:
        self._ensure_usable()
        self.transactions.begin(self.db.clone())
        return {"status": "ok", "message": "Transaction started"}

    def commit(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self.transactions.commit()
        try:
            if self.storage is not None:
                self.storage.save(self.db)
        except AmbiguousCommitError:
            self._recovery_required = True
            raise
        except Exception:
            # The commit never became durable before WAL COMMIT fsync, so memory
            # must match the previously durable state captured at BEGIN.
            self._install(transaction.snapshot)
            raise
        return {"status": "ok", "message": "Transaction committed"}

    def rollback(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self.transactions.rollback()
        self._install(transaction.snapshot)
        return {"status": "ok", "message": "Transaction rolled back"}

    def close(self) -> None:
        if not self._closed:
            if self._recovery_required:
                self._closed = True
                return
            if self.transactions.in_transaction:
                # An uncommitted transaction must never become durable.
                self.rollback()
            if self.storage is not None:
                self.storage.cleanup_if_clean()
            self._closed = True

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

