"""Explicit transaction state for AayushSQL.

The model is deliberately single-process and single-writer: at most one explicit
transaction exists per engine instance. ``BEGIN`` captures a deep snapshot of the
in-memory database (rows and index trees); statements mutate memory only; ``COMMIT``
performs the single durable save; ``ROLLBACK`` reinstalls the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ExecutionError


@dataclass
class Transaction:
    """One explicit transaction and the state it started from."""

    snapshot: Any
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

    def begin(self, snapshot: Any) -> None:
        if self.active is not None:
            raise ExecutionError(
                "Nested transactions are not supported; COMMIT or ROLLBACK the active transaction first"
            )
        self.active = Transaction(snapshot=snapshot)
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
