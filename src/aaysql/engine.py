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
from .locking import DatabaseLease
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

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        _lease: DatabaseLease | None = None,
    ):
        """Open a database, taking exclusive ownership when it is file-backed.

        A normal file-backed construction acquires its own :class:`DatabaseLease`
        and releases it on :meth:`close`. Internal owners (server, backup,
        restore) pass ``_lease=<their active lease>`` so the engine **borrows**
        ownership instead of taking it a second time; the engine then never
        releases that lease. In-memory engines take no lease at all.
        """
        self.lexer = Lexer()
        self._lease: DatabaseLease | None = None
        self._owns_lease = False
        if db_path is None or str(db_path) == ":memory:":
            if _lease is not None:
                raise StorageError("an in-memory Engine cannot borrow a database lease")
            self.db_path: Path | None = None
            self.storage: JsonFileStorage | None = None
            self.db = Database()
        else:
            if isinstance(db_path, str) and not db_path.strip():
                raise StorageError("Database path cannot be empty")
            if _lease is not None:
                if not isinstance(_lease, DatabaseLease):
                    raise StorageError("borrowed lease must be a DatabaseLease")
                _lease.validate_borrow(db_path)
                self._lease = _lease
                canonical = _lease.db_path
            else:
                lease = DatabaseLease.for_database(db_path)
                lease.acquire()
                self._lease = lease
                self._owns_lease = True
                canonical = lease.db_path
            assert canonical is not None
            self.db_path = canonical
            try:
                self.storage = JsonFileStorage(self.db_path)
                self.db = self.storage.load()
            except BaseException:
                # Never leave ownership behind when construction fails: opening
                # the database may already have recovered or checkpointed the WAL.
                self._release_owned_lease()
                raise
        self.executor = Executor(self.db)
        self.transactions = TransactionManager()
        self._closed = False
        self._recovery_required = False

    @property
    def is_persistent(self) -> bool:
        return self.storage is not None

    @property
    def lease(self) -> DatabaseLease | None:
        """The lease this engine uses, or ``None`` for an in-memory engine."""
        return self._lease

    @property
    def owns_lease(self) -> bool:
        """True when this engine acquired (and must release) its lease."""
        return self._owns_lease

    @property
    def in_transaction(self) -> bool:
        return self.transactions.in_transaction

    @property
    def needs_recovery(self) -> bool:
        return self._recovery_required

    @property
    def wal_poisoned(self) -> bool:
        """True when a failed WAL append left the log in a recovery-only state."""
        return self.storage is not None and self.storage.wal.poisoned

    def _ensure_usable(self) -> None:
        if self._closed:
            raise StorageError("Cannot execute statement on a closed Engine")
        if self._recovery_required:
            raise StorageError(
                "Database durability state is ambiguous after a WAL fsync failure; "
                "reopen the database to recover"
            )
        if self.storage is not None and self.storage.wal.poisoned:
            raise StorageError(
                "WAL is in a damaged state after a failed append; "
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
                if self.storage is not None and self.storage.wal.poisoned:
                    self._recovery_required = True
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
            # must match the previously durable state captured at BEGIN. If the
            # failed append may have left torn bytes, only a reopen can continue.
            self._install(transaction.snapshot)
            if self.storage is not None and self.storage.wal.poisoned:
                self._recovery_required = True
            raise
        return {"status": "ok", "message": "Transaction committed"}

    def rollback(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self.transactions.rollback()
        self._install(transaction.snapshot)
        return {"status": "ok", "message": "Transaction rolled back"}

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._recovery_required:
                return
            if self.transactions.in_transaction:
                # An uncommitted transaction must never become durable.
                self.rollback()
            if self.storage is not None:
                self.storage.cleanup_if_clean()
        finally:
            # Release even when rollback or cleanup fails: a leaked lease would
            # block every future owner of this database.
            self._closed = True
            self._release_owned_lease()

    def _release_owned_lease(self) -> None:
        """Release ownership *only* if this engine acquired it."""
        if not self._owns_lease or self._lease is None:
            return
        self._lease.release()
        self._lease = None
        self._owns_lease = False

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

