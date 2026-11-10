"""Secondary indexes: catalog entries backed by a B+ tree.

An index is an acceleration structure, never the source of truth: table rows stay
authoritative and every index can be rebuilt from them. NULL values are not
indexed because SQL comparisons against NULL never evaluate to TRUE, so a NULL
row can never qualify for an indexed predicate.
"""

from __future__ import annotations

import re
from typing import Any

from .btree import BPlusTree
from .errors import ExecutionError, StorageError

INDEX_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MISSING = object()


def is_valid_index_name(name: Any) -> bool:
    return isinstance(name, str) and bool(INDEX_NAME_PATTERN.fullmatch(name))


def validate_index_name(name: Any) -> str:
    """Validate an index name, raising ``ExecutionError`` when it is unusable."""
    if not is_valid_index_name(name):
        raise ExecutionError(
            f"Invalid index name {name!r}: use letters, digits, and underscores, "
            f"starting with a letter or underscore"
        )
    return name


class Index:
    """One single-column index over a table column."""

    def __init__(
        self,
        name: str,
        table: str,
        column: str,
        rows: Any = (),
    ) -> None:
        validate_index_name(name)
        self.name = name
        self.table = table
        self.column = column
        self.tree = BPlusTree()
        # Position of each row in table order. Index candidates are returned in
        # table order so an index scan produces the same row order as a table scan.
        self._order: dict[int, int] = {}
        self._next_order = 0
        self.build(rows)

    def build(self, rows: Any) -> None:
        """Rebuild the tree from authoritative rows."""
        self.tree = BPlusTree()
        self._order = {}
        self._next_order = 0
        for row in rows:
            self.add(row)

    def add(self, row: dict[str, Any]) -> None:
        if id(row) not in self._order:
            self._order[id(row)] = self._next_order
            self._next_order += 1
        value = row.get(self.column)
        if value is not None:
            self.tree.insert(value, row)

    def remove(self, row: dict[str, Any], key: Any = _MISSING, forget: bool = True) -> None:
        """Remove ``row`` from the tree.

        Pass ``key`` with the previous column value when the indexed column changed.
        ``forget=False`` keeps the row's table position for an in-place UPDATE.
        """
        if forget:
            self._order.pop(id(row), None)
        value = row.get(self.column) if key is _MISSING else key
        if value is None:
            return
        try:
            self.tree.delete(value, row)
        except KeyError as exc:
            raise StorageError(
                f"Index '{self.name}' is inconsistent with table '{self.table}': "
                f"no entry for {self.column}={value!r}"
            ) from exc

    def _in_table_order(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: self._order[id(row)])

    def lookup(self, value: Any) -> list[dict[str, Any]]:
        """Rows whose indexed column equals ``value``, in table order."""
        if value is None:
            return []
        return self._in_table_order(list(self.tree.search(value)))

    def range(
        self,
        start: Any = None,
        end: Any = None,
        start_inclusive: bool = True,
        end_inclusive: bool = True,
    ) -> list[dict[str, Any]]:
        """Rows whose indexed column falls inside the given bounds.

        ``None`` means "unbounded" on that side; NULL values are never indexed, so
        this cannot be confused with a NULL bound.
        """
        if start is None and end is None:
            rows = [row for _, row in self.tree.scan()]
        elif start is None:
            rows = [row for _, row in self.tree.scan(end=end, end_inclusive=end_inclusive)]
        elif end is None:
            rows = [
                row
                for _, row in self.tree.scan(start=start, start_inclusive=start_inclusive)
            ]
        else:
            rows = [
                row
                for _, row in self.tree.scan(
                    start=start,
                    end=end,
                    start_inclusive=start_inclusive,
                    end_inclusive=end_inclusive,
                )
            ]
        return self._in_table_order(rows)

    def validate(self) -> None:
        self.tree.validate()

    def __len__(self) -> int:
        return len(self.tree)

    def describe(self) -> dict[str, str]:
        return {"table": self.table, "column": self.column}
