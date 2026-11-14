"""In-memory undo journal for transaction and statement rollback.

The durable format-v4 WAL records *forward* changes so a committed transaction
survives a crash. This module records *inverse* changes so an uncommitted
transaction (or one failed statement) can be reversed in memory without copying
the database.

The journal is an append-only list of inverse entries plus integer markers::

    marker = journal.mark()      # O(1), just len(journal)
    journal.record(entry)        # before the forward mutation
    journal.rollback_to(marker)  # apply entries in reverse, then drop them
    journal.discard_to(marker)   # successful commit: drop without applying

Every entry stores only what is needed to reverse one storage-level mutation:
inserted rows or their positions, an updated row's pre-image, deleted rows and
their positions, or a detached table/index object for DDL. The journal is never
serialized and is not a second WAL.

Rows are mutable dictionaries and indexes identify rows by object identity, so
undo mutates row dictionaries in place and applies exact inverse index
operations instead of rebuilding index trees. ``DELETE`` rollback re-inserts the
captured row objects at their original positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ExecutionError


class UndoError(Exception):
    """Raised when an undo entry cannot be applied safely."""


@dataclass(slots=True)
class InsertUndo:
    """Remove the exact rows appended by one INSERT."""

    table: str
    start: int
    rows: list[dict[str, Any]]

    def apply(self, db: Any) -> None:
        try:
            table = db.table(self.table)
        except ExecutionError as exc:
            raise UndoError(f"undo INSERT: unknown table '{self.table}'") from exc
        end = self.start + len(self.rows)
        if end > len(table.rows):
            raise UndoError(
                f"undo INSERT: table '{self.table}' has {len(table.rows)} rows, "
                f"expected at least {end}"
            )
        inserted = table.rows[self.start:end]
        for index in db.indexes_on(self.table):
            for row in inserted:
                index.discard(row)
        del table.rows[self.start:end]
        table.touch()


@dataclass(slots=True)
class UpdateUndo:
    """Restore one row's pre-update values at its table position."""

    table: str
    position: int
    before: dict[str, Any]

    def apply(self, db: Any) -> None:
        try:
            table = db.table(self.table)
        except ExecutionError as exc:
            raise UndoError(f"undo UPDATE: unknown table '{self.table}'") from exc
        if self.position >= len(table.rows):
            raise UndoError(
                f"undo UPDATE: position {self.position} is out of range for "
                f"table '{self.table}' ({len(table.rows)} rows)"
            )
        row = table.rows[self.position]
        indexes = db.indexes_on(self.table)
        # Row values are still the post-update ones here, so drop the current
        # index entry (tolerantly: an injected failure may have interrupted the
        # forward index update) and then restore the old key.
        for index in indexes:
            index.discard(row, forget=False)
        from .json_value import deep_copy_json

        row.update(deep_copy_json(self.before))
        for index in indexes:
            index.ensure(row)
        table.touch()


@dataclass(slots=True)
class DeleteUndo:
    """Re-insert deleted rows at their original positions."""

    table: str
    entries: list[tuple[int, dict[str, Any]]]

    def apply(self, db: Any) -> None:
        try:
            table = db.table(self.table)
        except ExecutionError as exc:
            raise UndoError(f"undo DELETE: unknown table '{self.table}'") from exc
        ordered = sorted(self.entries, key=lambda entry: entry[0])
        # Restoring several rows at once is valid when each position is within
        # the table as previous (earlier) entries have already been re-inserted.
        length = len(table.rows)
        for position, _row in ordered:
            if position > length:
                raise UndoError(
                    f"undo DELETE: position {position} is out of range for "
                    f"table '{self.table}' ({length} rows while restoring)"
                )
            length += 1
        for position, row in ordered:
            table.rows.insert(position, row)
        for index in db.indexes_on(self.table):
            index.restore_positions(ordered)
        table.touch()


@dataclass(slots=True)
class CreateTableUndo:
    """Drop a table created earlier in the transaction."""

    table: str

    def apply(self, db: Any) -> None:
        if self.table not in db.tables:
            raise UndoError(f"undo CREATE TABLE: table '{self.table}' is missing")
        del db.tables[self.table]
        db.stats.invalidate(self.table)


@dataclass(slots=True)
class DropTableUndo:
    """Restore a detached table object and the indexes that belonged to it."""

    table: str
    table_object: Any
    indexes: list[Any] = field(default_factory=list)

    def apply(self, db: Any) -> None:
        if self.table in db.tables:
            raise UndoError(
                f"undo DROP TABLE: table '{self.table}' already exists"
            )
        db.tables[self.table] = self.table_object
        for index in self.indexes:
            db.indexes[index.name] = index
        db.stats.invalidate(self.table)


@dataclass(slots=True)
class CreateIndexUndo:
    """Drop an index created earlier in the transaction."""

    name: str

    def apply(self, db: Any) -> None:
        if self.name not in db.indexes:
            raise UndoError(f"undo CREATE INDEX: index '{self.name}' is missing")
        del db.indexes[self.name]


@dataclass(slots=True)
class DropIndexUndo:
    """Restore a detached index definition and its tree."""

    name: str
    index_object: Any

    def apply(self, db: Any) -> None:
        if self.name in db.indexes:
            raise UndoError(f"undo DROP INDEX: index '{self.name}' already exists")
        db.indexes[self.name] = self.index_object


UndoEntry = (
    InsertUndo
    | UpdateUndo
    | DeleteUndo
    | CreateTableUndo
    | DropTableUndo
    | CreateIndexUndo
    | DropIndexUndo
)


class UndoJournal:
    """Append-only in-memory inverse journal with integer markers."""

    def __init__(self) -> None:
        self._entries: list[UndoEntry] = []
        self._replaying = False

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def replaying(self) -> bool:
        return self._replaying

    def mark(self) -> int:
        """Return an O(1) position that later rollbacks can restore."""
        return len(self._entries)

    def record(self, entry: UndoEntry) -> None:
        """Append an inverse entry before the forward mutation is applied."""
        if self._replaying:
            raise RuntimeError("cannot record undo entries while rolling back")
        self._entries.append(entry)

    def rollback_to(self, marker: int, db: Any) -> None:
        """Apply entries after ``marker`` in reverse order, then drop them."""
        self._validate(marker)
        while len(self._entries) > marker:
            entry = self._entries[-1]
            self._replaying = True
            try:
                entry.apply(db)
            finally:
                self._replaying = False
            self._entries.pop()

    def discard_to(self, marker: int) -> None:
        """Drop entries after ``marker`` without applying them (commit path)."""
        self._validate(marker)
        del self._entries[marker:]

    def clear(self) -> None:
        self._entries.clear()

    def _validate(self, marker: Any) -> None:
        if isinstance(marker, bool) or not isinstance(marker, int):
            raise TypeError("undo marker must be an integer")
        if marker < 0 or marker > len(self._entries):
            raise ValueError("undo marker is out of range")
