"""Secondary indexes: catalog entries backed by a B+ tree.

An index is an acceleration structure, never the source of truth: table rows stay
authoritative and every index can be rebuilt from them. NULL values are not
indexed because SQL comparisons against NULL never evaluate to TRUE, so a NULL
row can never qualify for an indexed predicate.

Supports:
- Ordinary single-column indexes over relational table columns.
- Scalar JSON path indexes over JSON / DOCUMENT columns (ADR-13) using
  typed memcomparable key encoding.
"""

from __future__ import annotations

import re
from typing import Any

from .btree import BPlusTree
from .errors import ExecutionError, StorageError
from .index_encoding import encode_value_prefix, is_indexable_scalar
from .json_value import ABSENT, JSON_NULL, extract_path

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
    """One single-column or scalar JSON path index over a table."""

    def __init__(
        self,
        name: str,
        table: str,
        column: str,
        rows: Any = (),
        path: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        validate_index_name(name)
        self.name = name
        self.table = table
        self.column = column
        self.path = tuple(path) if path is not None else None
        self.tree = BPlusTree()
        # Position of each row in table order. Index candidates are returned in
        # table order so an index scan produces the same row order as a table scan.
        self._order: dict[int, int] = {}
        self._next_order = 0
        self.build(rows)

    @property
    def is_path_index(self) -> bool:
        return self.path is not None

    def extract_key(self, row: dict[str, Any]) -> Any:
        raw = row.get(self.column)
        if self.path is None:
            return raw
        if raw is None or raw is ABSENT or raw is JSON_NULL or not isinstance(raw, (dict, list)):
            return ABSENT
        return extract_path(raw, self.path)

    def is_indexable(self, value: Any) -> bool:
        if self.path is None:
            return value is not None
        return is_indexable_scalar(value)

    def encode_key(self, value: Any) -> Any:
        if self.path is None:
            return value
        return encode_value_prefix(value)

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
        value = self.extract_key(row)
        if self.is_indexable(value):
            enc_key = self.encode_key(value)
            self.tree.insert(enc_key, row)

    def discard(
        self, row: dict[str, Any], key: Any = _MISSING, forget: bool = True
    ) -> None:
        """Remove ``row`` if it is present; never raise when it is absent.

        Undo paths need this: a statement that failed part-way through index
        maintenance may not have inserted or removed the entry it intended to.
        """
        value = self.extract_key(row) if key is _MISSING else key
        if self.is_indexable(value):
            enc_key = self.encode_key(value)
            try:
                self.tree.delete(enc_key, row)
            except KeyError:
                pass
        if forget:
            self._order.pop(id(row), None)

    def ensure(self, row: dict[str, Any]) -> None:
        """Insert ``row`` under its current key unless that entry already exists."""
        value = self.extract_key(row)
        if not self.is_indexable(value):
            return
        if id(row) not in self._order:
            self._order[id(row)] = self._next_order
            self._next_order += 1
        enc_key = self.encode_key(value)
        for existing in self.tree.search(enc_key):
            if existing is row:
                return
        self.tree.insert(enc_key, row)

    def restore_positions(
        self, entries: list[tuple[int, dict[str, Any]]]
    ) -> None:
        """Re-add deleted ``(position, row)`` pairs in original table order.

        ``_order`` records each row's position in table order and is never
        shifted when a row is deleted, so restoring the captured position is
        enough to make index scans agree with table scans again.
        """
        ordered = sorted(entries, key=lambda entry: entry[0])
        for _position, row in ordered:
            self.discard(row)
        for position, row in ordered:
            self._order[id(row)] = position
            value = self.extract_key(row)
            if self.is_indexable(value):
                enc_key = self.encode_key(value)
                self.tree.insert(enc_key, row)
        if ordered:
            self._next_order = max(self._next_order, ordered[-1][0] + 1)

    def remove(self, row: dict[str, Any], key: Any = _MISSING, forget: bool = True) -> None:
        """Remove ``row`` from the tree.

        Pass ``key`` with the previous column value when the indexed column changed.
        ``forget=False`` keeps the row's table position for an in-place UPDATE.
        """
        if forget:
            self._order.pop(id(row), None)
        value = self.extract_key(row) if key is _MISSING else key
        if not self.is_indexable(value):
            return
        enc_key = self.encode_key(value)
        try:
            self.tree.delete(enc_key, row)
        except KeyError as exc:
            raise StorageError(
                f"Index '{self.name}' is inconsistent with table '{self.table}': "
                f"no entry for {self.column}={value!r}"
            ) from exc

    def _in_table_order(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: self._order[id(row)])

    def lookup(self, value: Any) -> list[dict[str, Any]]:
        """Rows whose indexed column or path equals ``value``, in table order."""
        if not self.is_indexable(value):
            return []
        enc_key = self.encode_key(value)
        return self._in_table_order(list(self.tree.search(enc_key)))

    def range(
        self,
        start: Any = None,
        end: Any = None,
        start_inclusive: bool = True,
        end_inclusive: bool = True,
    ) -> list[dict[str, Any]]:
        """Rows whose indexed column or path falls inside the given bounds.

        ``None`` means "unbounded" on that side.
        """
        if self.path is not None:
            start_key = self.encode_key(start) if start is not None and self.is_indexable(start) else None
            end_key = self.encode_key(end) if end is not None and self.is_indexable(end) else None
        else:
            start_key = start
            end_key = end

        if start_key is None and end_key is None:
            rows = [row for _, row in self.tree.scan()]
        elif start_key is None:
            rows = [row for _, row in self.tree.scan(end=end_key, end_inclusive=end_inclusive)]
        elif end_key is None:
            rows = [
                row
                for _, row in self.tree.scan(start=start_key, start_inclusive=start_inclusive)
            ]
        else:
            rows = [
                row
                for _, row in self.tree.scan(
                    start=start_key,
                    end=end_key,
                    start_inclusive=start_inclusive,
                    end_inclusive=end_inclusive,
                )
            ]
        return self._in_table_order(rows)

    def validate(self) -> None:
        self.tree.validate()

    def __len__(self) -> int:
        return len(self.tree)

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"table": self.table, "column": self.column}
        if self.path is not None:
            d["path"] = list(self.path)
        return d
