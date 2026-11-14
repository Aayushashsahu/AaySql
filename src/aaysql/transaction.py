"""Explicit transaction state for AayushSQL.

The model is deliberately single-process and single-writer: at most one explicit
transaction exists per engine instance. ``BEGIN`` records an O(1) marker in the
in-memory undo journal (no database copy); statements mutate memory only;
``COMMIT`` performs the single durable save; ``ROLLBACK`` applies the journal's
inverse entries in reverse order.

The journal is in-memory rollback state, not durable storage: the durable record
of a committed transaction is still the delta WAL written at ``COMMIT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ExecutionError


@dataclass
class Transaction:
    """One explicit transaction and the journal position it started at."""

    undo_marker: int
    dirty: bool = False
    statements: int = 0


@dataclass
class TransactionManager:
    """Tracks the optional active transaction for one engine instance."""

    active: Transaction | None = None
    _begun: int = field(default=0, init=False, repr=False)

    @property
    def in_transaction(self) -> bool:
        return self.active is not None

    def begin(self, undo_marker: int) -> None:
        if self.active is not None:
            raise ExecutionError(
                "Nested transactions are not supported; COMMIT or ROLLBACK the active transaction first"
            )
        self.active = Transaction(undo_marker=undo_marker)
        self._begun += 1

    def record_statement(self) -> None:
        if self.active is not None:
            self.active.dirty = True
            self.active.statements += 1

    def commit(self) -> Transaction:
        if self.active is None:
            raise ExecutionError("COMMIT without an active transaction")
        transaction = self.active
        self.active = None
        return transaction

    def rollback(self) -> Transaction:
        if self.active is None:
            raise ExecutionError("ROLLBACK without an active transaction")
        transaction = self.active
        self.active = None
        return transaction
