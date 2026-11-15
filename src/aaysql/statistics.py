"""In-memory table statistics used by the cost-based planner.

Statistics are derived from table rows only -- never from an index -- so they are
identical with and without indexes. They are descriptive estimates, never the
source of truth:

* row counts and NULL counts are always exact (computed by one full pass);
* distinct counts are exact up to ``MAX_EXACT_DISTINCT`` values per column and
  are otherwise estimated from a fixed-size hashed bitmap, which keeps memory
  bounded no matter how wide the value space is;
* min/max are recorded only when the observed values are mutually comparable.

Every cached entry is keyed by table name *and* validated against the live table
object plus its row revision counter. ``INSERT``/``UPDATE``/``DELETE`` bump that
counter, and rollback/undo also bumps it, so a stale entry can never survive a
mutation or a rolled-back transaction.
"""

from __future__ import annotations

import hashlib
import math
import weakref
from dataclasses import dataclass, field
from typing import Any

from .json_value import ABSENT, JSON_NULL, get_path

MAX_EXACT_DISTINCT = 4096
_DISTINCT_BITMAP_BITS = 8192

METHOD = (
    "full table scan: row and NULL counts exact; distinct counts exact up to "
    f"{MAX_EXACT_DISTINCT} values and otherwise estimated from a "
    f"{_DISTINCT_BITMAP_BITS}-bit hashed bitmap; min/max only when comparable"
)


def _bit_index(value: Any) -> int:
    payload = repr((type(value).__name__, value)).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") % _DISTINCT_BITMAP_BITS


def _popcount(bits: bytearray) -> int:
    return sum(byte.bit_count() for byte in bits)


class _DistinctCounter:
    """Exact distinct tracking that degrades to a bounded bitmap estimate."""

    __slots__ = ("_bits", "_count", "_exact", "_exact_mode")

    def __init__(self) -> None:
        self._exact: set[tuple[str, Any]] = set()
        self._bits: bytearray | None = None
        self._count = 0
        self._exact_mode = True

    def add(self, value: Any) -> None:
        if isinstance(value, (dict, list)):
            key = (type(value).__name__, repr(value))
        else:
            key = (type(value).__name__, value)
        if self._exact_mode:
            if key in self._exact:
                return
            self._exact.add(key)
            self._count = len(self._exact)
            if self._count > MAX_EXACT_DISTINCT:
                self._switch_to_bitmap()
            return
        assert self._bits is not None
        index = _bit_index(key[1])
        self._bits[index >> 3] |= 1 << (index & 7)

    def _switch_to_bitmap(self) -> None:
        bits = bytearray(_DISTINCT_BITMAP_BITS // 8)
        for _, value in self._exact:
            index = _bit_index(value)
            bits[index >> 3] |= 1 << (index & 7)
        # The exact set is dropped so memory stays bounded from here on.
        self._exact = set()
        self._exact_mode = False
        self._bits = bits
        self._count = self._estimate()

    def _estimate(self) -> int:
        assert self._bits is not None
        zeros = _DISTINCT_BITMAP_BITS - _popcount(self._bits)
        if zeros <= 0:
            return _DISTINCT_BITMAP_BITS * 2
        estimate = -_DISTINCT_BITMAP_BITS * math.log(zeros / _DISTINCT_BITMAP_BITS)
        return max(1, round(estimate))

    @property
    def estimate(self) -> int:
        if self._exact_mode:
            return self._count
        return self._estimate()

    @property
    def is_exact(self) -> bool:
        return self._exact_mode


@dataclass(frozen=True)
class ColumnStats:
    """Estimates for one column of one table."""

    name: str
    null_count: int
    distinct: int
    distinct_is_exact: bool
    minimum: Any = None
    maximum: Any = None
    min_max_available: bool = False


@dataclass(frozen=True)
class PathStats:
    """Estimates for one scalar JSON path of one column."""

    column: str
    path: tuple[str, ...]
    present_count: int
    null_count: int
    missing_count: int
    distinct: int
    distinct_is_exact: bool
    minimum: Any = None
    maximum: Any = None
    min_max_available: bool = False


@dataclass(frozen=True)
class TableStats:
    """Estimates for one table, tagged with the revision they describe."""

    table: str
    revision: int
    row_count: int
    columns: dict[str, Any]
    paths: dict[tuple[str, tuple[str, ...]], PathStats] = field(default_factory=dict)
    method: str = METHOD

    def column(self, name: str) -> Any | None:
        return self.columns.get(name)

    def path(self, column: str, path_segments: tuple[str, ...] | list[str]) -> PathStats | None:
        return self.paths.get((column, tuple(path_segments)))


def compute_table_stats(table: Any, indexes: list[Any] | None = None) -> TableStats:
    """Compute statistics for ``table`` with a single bounded-memory pass."""
    names = [column.name for column in table.columns]
    counters = {name: _DistinctCounter() for name in names}
    nulls = {name: 0 for name in names}
    minimum: dict[str, Any] = {name: None for name in names}
    maximum: dict[str, Any] = {name: None for name in names}
    comparable = {name: True for name in names}
    seen = {name: False for name in names}

    target_paths: set[tuple[str, tuple[str, ...]]] = set()
    if indexes:
        for idx in indexes:
            if getattr(idx, "path", None) and getattr(idx, "column", None):
                target_paths.add((idx.column, tuple(idx.path)))

    path_counters = {p: _DistinctCounter() for p in target_paths}
    path_nulls = {p: 0 for p in target_paths}
    path_missing = {p: 0 for p in target_paths}
    path_present = {p: 0 for p in target_paths}
    path_min: dict[tuple[str, tuple[str, ...]], Any] = {p: None for p in target_paths}
    path_max: dict[tuple[str, tuple[str, ...]], Any] = {p: None for p in target_paths}
    path_comparable = {p: True for p in target_paths}
    path_seen = {p: False for p in target_paths}

    for row in table.rows:
        for name in names:
            value = row.get(name)
            if value is None:
                nulls[name] += 1
                continue
            counters[name].add(value)
            if not seen[name]:
                minimum[name] = value
                maximum[name] = value
                seen[name] = True
                continue
            if not comparable[name]:
                continue
            try:
                minimum[name] = min(minimum[name], value)
                maximum[name] = max(maximum[name], value)
            except TypeError:
                # Mixed/complex values: stop claiming an ordering range.
                comparable[name] = False
                minimum[name] = None
                maximum[name] = None

        for col_name, path_tuple in target_paths:
            p_key = (col_name, path_tuple)
            doc = row.get(col_name)
            pval = get_path(doc, path_tuple)
            if pval is ABSENT:
                path_missing[p_key] += 1
            elif pval is None or pval is JSON_NULL:
                path_nulls[p_key] += 1
            else:
                path_present[p_key] += 1
                path_counters[p_key].add(pval)
                if not path_seen[p_key]:
                    path_min[p_key] = pval
                    path_max[p_key] = pval
                    path_seen[p_key] = True
                elif path_comparable[p_key]:
                    try:
                        path_min[p_key] = min(path_min[p_key], pval)
                        path_max[p_key] = max(path_max[p_key], pval)
                    except TypeError:
                        path_comparable[p_key] = False
                        path_min[p_key] = None
                        path_max[p_key] = None

    columns: dict[str, ColumnStats] = {}
    for name in names:
        counter = counters[name]
        estimate = counter.estimate
        non_null = len(table.rows) - nulls[name]
        columns[name] = ColumnStats(
            name=name,
            null_count=nulls[name],
            distinct=min(estimate, max(non_null, 0)) if non_null else 0,
            distinct_is_exact=counter.is_exact,
            minimum=minimum[name],
            maximum=maximum[name],
            min_max_available=comparable[name] and seen[name],
        )

    paths: dict[tuple[str, tuple[str, ...]], PathStats] = {}
    for p_key in target_paths:
        counter = path_counters[p_key]
        estimate = counter.estimate
        present = path_present[p_key]
        paths[p_key] = PathStats(
            column=p_key[0],
            path=p_key[1],
            present_count=present,
            null_count=path_nulls[p_key],
            missing_count=path_missing[p_key],
            distinct=min(estimate, max(present, 0)) if present else 0,
            distinct_is_exact=counter.is_exact,
            minimum=path_min[p_key],
            maximum=path_max[p_key],
            min_max_available=path_comparable[p_key] and path_seen[p_key],
        )

    return TableStats(
        table=table.name,
        revision=getattr(table, "revision", 0),
        row_count=len(table.rows),
        columns=columns,
        paths=paths,
    )


class StatisticsStore:
    """Per-database cache of table statistics, invalidated by row revisions."""

    def __init__(self, db: Any = None) -> None:
        self._db_ref = weakref.ref(db) if db is not None else None
        self._entries: dict[str, tuple[weakref.ref[Any], int, TableStats]] = {}

    def invalidate(self, table_name: str | None = None) -> None:
        if table_name is None:
            self._entries.clear()
        else:
            self._entries.pop(table_name, None)

    def get(self, table: Any, indexes: list[Any] | None = None) -> TableStats:
        """Return statistics for ``table``, recomputing after any row change."""
        entry = self._entries.get(table.name)
        revision = getattr(table, "revision", 0)
        if entry is not None:
            ref, cached_revision, stats = entry
            if ref() is table and cached_revision == revision:
                return stats
        if indexes is None and self._db_ref is not None:
            db = self._db_ref()
            if db is not None:
                indexes = db.indexes_on(table.name)
        stats = compute_table_stats(table, indexes=indexes)
        self._entries[table.name] = (weakref.ref(table), revision, stats)
        return stats

    def __len__(self) -> int:
        return len(self._entries)
