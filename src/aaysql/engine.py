from __future__ import annotations

import weakref
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
from .delta import MutationRecorder
from .errors import AmbiguousCommitError, StorageError
from .executor import Executor
from .lexer import Lexer
from .locking import DatabaseLease
from .mvcc import DatabaseSession, IsolationLevel, MvccStore
from .parser import Parser
from .storage import Database, open_storage
from .transaction import TransactionManager
from .undo import UndoJournal

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
        self._recorder = MutationRecorder()
        self._journal = UndoJournal()
        self._lease: DatabaseLease | None = None
        self._owns_lease = False
        if db_path is None or str(db_path) == ":memory:":
            if _lease is not None:
                raise StorageError("an in-memory Engine cannot borrow a database lease")
            self.db_path: Path | None = None
            self.storage: Any | None = None
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
                self.storage = open_storage(self.db_path)
                self.db = self.storage.load()
            except BaseException:
                # Never leave ownership behind when construction fails: opening
                # the database may already have recovered or checkpointed the WAL.
                self._release_owned_lease()
                raise
        self.executor = Executor(
            self.db, recorder=self._recorder, journal=self._journal
        )
        self.transactions = TransactionManager()
        self._closed = False
        self._recovery_required = False
        self._legacy_dirty = False
        self._sessions: weakref.WeakSet[DatabaseSession] = weakref.WeakSet()
        self._default_session: DatabaseSession | None = None
        self.mvcc = MvccStore(self.db, self.storage)
        self.mvcc.set_publish_hook(self._publish_root)

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
        return self._recovery_required or self.mvcc.failed is not None

    @property
    def wal_poisoned(self) -> bool:
        """True when a failed WAL append left the log in a recovery-only state."""
        return (self.storage is not None and self.storage.wal.poisoned) or (
            self.mvcc.failed is not None
        )

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

    def connect(
        self, isolation: IsolationLevel | str | None = None
    ) -> DatabaseSession:
        """Open an independent MVCC session on this engine.

        Sessions may read while another session writes. Each session owns its
        transaction state; only one writer is admitted at a time, and commits are
        durably ordered by the group-commit queue.
        """
        self._ensure_usable()
        if self._legacy_dirty:
            self.mvcc.sync_legacy(self.db)
            self._legacy_dirty = False
        session = DatabaseSession(self.mvcc, isolation=isolation, lexer=self.lexer)
        self._sessions.add(session)
        return session

    def _publish_root(self, db: Database) -> None:
        """Make a session-published root the engine's current database view."""
        self.db = db
        self.executor.db = db
        self._legacy_dirty = False

    @property
    def undo_entries(self) -> int:
        """Number of pending inverse entries (inspection/tests only)."""
        return len(self._journal)

    def execute(self, sql: str) -> dict[str, Any]:
        self._ensure_usable()
        if self._default_session is not None:
            result = self._default_session.execute(sql)
            if not self._default_session.in_transaction:
                self._default_session.close()
                self._default_session = None
            return result
        tokens = self.lexer.tokenize(sql)
        stmt = Parser(tokens).parse()

        if isinstance(stmt, Begin):
            if stmt.isolation is not None:
                # Explicit isolation levels need a snapshot-owning session; the
                # legacy mutable engine remains the default READ COMMITTED path.
                self._default_session = self.connect(stmt.isolation)
                return self._default_session.execute(sql)
            return self.begin()
        if isinstance(stmt, Commit):
            return self.commit()
        if isinstance(stmt, Rollback):
            return self.rollback()

        if self.transactions.in_transaction:
            return self._execute_in_transaction(stmt)

        if isinstance(stmt, _MUTATION_STATEMENTS):
            return self._execute_autocommit(stmt)
        return self.executor.execute(stmt)

    def _execute_autocommit(self, stmt: Any) -> dict[str, Any]:
        """Run one independent mutating statement with statement atomicity.

        The undo journal reverses partial effects on failure. When the statement
        is durable, its inverse entries are discarded only after the storage
        layer has classified the commit; an ambiguous COMMIT is never treated as
        a rollback.
        """
        persistent = self.storage is not None
        mark = self._journal.mark()
        if persistent:
            self._recorder.clear()
            self._recorder.start()
        try:
            result = self.executor.execute(stmt)
            if persistent:
                changes = self._recorder.take()
                self.storage.save(self.db, changes)
        except AmbiguousCommitError:
            if persistent:
                self._recorder.take()
            # The commit may be durable. Keep the WAL for recovery and never
            # apply inverse entries as though the transaction had rolled back.
            self._journal.clear()
            self._recovery_required = True
            raise
        except Exception:
            if persistent:
                self._recorder.take()
            self._journal.rollback_to(mark, self.db)
            if persistent and self.storage.wal.poisoned:
                self._recovery_required = True
            raise
        self._journal.discard_to(mark)
        self._legacy_dirty = True
        return result

    def _execute_in_transaction(self, stmt: Any) -> dict[str, Any]:
        if not isinstance(stmt, _MUTATION_STATEMENTS):
            return self.executor.execute(stmt)
        # Each statement stays atomic inside the transaction: a failed statement
        # undoes only its own inverse entries, leaving earlier statements alone.
        mark = self._journal.mark()
        recorder_mark = self._recorder.mark()
        try:
            result = self.executor.execute(stmt)
        except Exception:
            self._journal.rollback_to(mark, self.db)
            self._recorder.rollback_to(recorder_mark)
            raise
        self.transactions.record_statement()
        return result

    def begin(self) -> dict[str, Any]:
        self._ensure_usable()
        self.transactions.begin(self._journal.mark())
        self._recorder.clear()
        self._recorder.start()
        return {"status": "ok", "message": "Transaction started"}

    def commit(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self.transactions.commit()
        marker = transaction.undo_marker
        try:
            changes = self._recorder.take()
            if self.storage is not None:
                self.storage.save(self.db, changes)
        except AmbiguousCommitError:
            # The transaction may be durable. Reopen recovery decides; do not
            # discard the WAL or apply the undo journal.
            self._journal.clear()
            self._recovery_required = True
            raise
        except Exception:
            # The commit never became durable before WAL COMMIT fsync, so reverse
            # the transaction's mutations in memory. If the failed append may
            # have left torn bytes, only a reopen can continue.
            self._recorder.take()
            self._journal.rollback_to(marker, self.db)
            if self.storage is not None and self.storage.wal.poisoned:
                self._recovery_required = True
            raise
        # Durable (or in-memory) commit: inverse entries are no longer needed.
        self._journal.discard_to(marker)
        self._legacy_dirty = True
        return {"status": "ok", "message": "Transaction committed"}

    def rollback(self) -> dict[str, Any]:
        self._ensure_usable()
        transaction = self.transactions.rollback()
        self._recorder.clear()
        self._journal.rollback_to(transaction.undo_marker, self.db)
        return {"status": "ok", "message": "Transaction rolled back"}

    def checkpoint(self) -> None:
        """Publish an explicit checkpoint of the current state.

        Only format-v4 databases have an explicit checkpoint; version 1-3
        databases are checkpointed by every commit.
        """
        self._ensure_usable()
        if self.storage is None:
            return
        self.storage.checkpoint(self.db)

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._default_session is not None:
                self._default_session.close()
                self._default_session = None
            for session in self._sessions:
                session.close()
            if self._recovery_required:
                return
            if self.transactions.in_transaction:
                # An uncommitted transaction must never become durable.
                self.rollback()
            if self.storage is not None:
                self.storage.cleanup_if_clean(self.db)
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

