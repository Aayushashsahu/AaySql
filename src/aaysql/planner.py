"""Cost-based query planning.

Planning happens in two explicit stages:

**Logical planning** decides *what* must be computed. It binds every source,
keeps the SQL join order and join types exactly as written, splits the ``WHERE``
and every ``ON`` clause into top-level ``AND`` conjuncts, and assigns each
conjunct to the earliest stage where it is provably equivalent to evaluate it.
Predicates are never split on ``OR``, never detached from the relations they
mention, and predicates over the nullable side of a ``LEFT JOIN`` are kept after
the join.

**Physical planning** decides *how* to compute it: table scan vs. B+ tree index
scan, hash join vs. nested-loop join, plus the aggregate / projection / sort /
limit wrappers. Every node records an estimated row count and cost from
in-memory table statistics and labels them as estimates.

The executor runs the physical plan produced here. The nested-loop join and the
table scan remain the reference semantics: both are always available (they can
be forced) and every alternative must produce the same rows in the same order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .ast import (
    AggregateCall,
    Between,
    BinaryExpr,
    Case,
    FunctionCall,
    Identifier,
    InList,
    IsJsonNull,
    IsMissing,
    IsNull,
    Like,
    Literal,
    PathAccess,
    Select,
    UnaryExpr,
)
from .errors import ExecutionError
from .expressions import (
    SourceBinding,
    contains_aggregate,
    resolve_identifier,
    resolve_path_access,
    walk_expr,
)
from .statistics import TableStats
from .storage import Database

_FLIPPED_OPS = {
    "=": "=",
    "!=": "!=",
    "<>": "<>",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}
_INDEX_OPS = {"=", "<", "<=", ">", ">="}

# Mirrors the parser's precedence table so EXPLAIN renders expressions that
# re-parse to the same tree.
_PRECEDENCE = {"OR": 1, "AND": 2}
_NOT_PRECEDENCE = 3
_COMPARISON_PRECEDENCE = 4
_ADDITIVE_PRECEDENCE = 5
_MULTIPLICATIVE_PRECEDENCE = 6
_UNARY_ARITHMETIC_PRECEDENCE = 7
_PRIMARY_PRECEDENCE = 8
_ARITHMETIC_PRECEDENCE = {
    "+": _ADDITIVE_PRECEDENCE,
    "-": _ADDITIVE_PRECEDENCE,
    "*": _MULTIPLICATIVE_PRECEDENCE,
    "/": _MULTIPLICATIVE_PRECEDENCE,
    "%": _MULTIPLICATIVE_PRECEDENCE,
}

# --- cost model constants -------------------------------------------------
#
# All costs are abstract "cost units", one unit being the cost of inspecting one
# row during a sequential scan. The constants are relative and were calibrated
# against ``benchmarks/benchmark_optimizer.py``; they never affect results, only
# which access path / join algorithm is chosen.
COST_SEQ_ROW = 0.8          # streaming one table row and evaluating predicates
COST_INDEX_SEEK = 0.2       # fixed B+ tree descent setup cost
COST_INDEX_LEVEL = 0.15     # per level of tree descent (~log2(rows))
COST_INDEX_ROW = 0.35       # candidate retrieval + table-order restoration
COST_INDEX_SORT = 0.045     # per candidate, times log2(candidates)
COST_HASH_BUILD = 0.6       # per build-side row inserted into the hash table
COST_HASH_PROBE = 0.5       # per probe-side row looked up
COST_HASH_MATCH = 0.1       # per candidate pair surviving the equality keys
COST_NL_COMPARE = 0.9       # per left/right pair compared by nested loop
# Below this input size the fixed hash-table setup is not worth modelling; small
# joins always keep the reference nested-loop implementation.
HASH_MIN_INPUT_ROWS = 16
# Default selectivity for a predicate the statistics cannot reason about.
DEFAULT_SELECTIVITY = 1.0 / 3.0

ESTIMATE_NOTE = (
    "ESTIMATES: ~row counts and costs are planner estimates from in-memory "
    "statistics, not measurements; plain EXPLAIN does not execute the query."
)


# --- plan nodes -----------------------------------------------------------


@dataclass
class PlanNode:
    """One physical operator in a plan tree."""

    op: str
    children: list[PlanNode] = field(default_factory=list)
    estimated_rows: float = 0.0
    estimated_cost: float = 0.0
    # scan nodes
    table: str | None = None
    ref: str | None = None
    index_name: str | None = None
    index_column: str | None = None
    condition: str | None = None
    start: Any = None
    end: Any = None
    start_inclusive: bool = True
    end_inclusive: bool = True
    predicates: list[Any] = field(default_factory=list)
    predicate_text: str | None = None
    # join nodes
    join_type: str | None = None
    algorithm: str | None = None
    on: Any = None
    on_text: str | None = None
    key_pairs: list[tuple[str, str, str, str]] = field(default_factory=list)
    why: str | None = None
    # wrapper nodes
    detail: str | None = None
    elided: bool = False
    # measured counters, filled in by the executor for EXPLAIN ANALYZE
    actual: dict[str, Any] = field(default_factory=dict)

    @property
    def is_scan(self) -> bool:
        return self.op in ("TABLE SCAN", "INDEX SCAN")


@dataclass
class JoinSpec:
    """A join as written in the query: order and type are never rewritten."""

    join_type: str
    condition: Any
    right_ref: str
    right_index: int
    left_refs: list[str]


@dataclass
class LogicalPlan:
    """Bindings and predicate placement, independent of any access method."""

    stmt: Select
    sources: list[SourceBinding]
    joins: list[JoinSpec]
    pushed: dict[str, list[Any]]
    post_filters: list[Any]
    is_grouped: bool
    has_group_by: bool
    has_order_by: bool
    limit: int | None
    distinct: bool = False
    offset: int | None = None
    predicates: dict[str, list[Any]] = field(default_factory=dict)
    on_predicates: dict[int, list[Any]] = field(default_factory=dict)
    projection_text: str | None = None
    sort_text: str | None = None
    group_text: str | None = None
    nullable_refs: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class QueryPlan:
    """The plan selected for one SELECT statement.

    ``root`` is the physical plan tree the executor runs. The scalar fields
    mirror the root access path so callers that only care about the base table
    keep working.
    """

    kind: str
    table: str
    index_name: str | None = None
    index_column: str | None = None
    condition: str | None = None
    filter: str | None = None
    start: Any = None
    end: Any = None
    start_inclusive: bool = True
    end_inclusive: bool = True
    joins: int = 0
    root: PlanNode | None = None


@dataclass
class IndexMatch:
    """An indexed predicate usable as a range scan."""

    index_name: str
    column: str
    predicate: Any
    start: Any = None
    end: Any = None
    start_inclusive: bool = True
    end_inclusive: bool = True


# --- expression helpers ---------------------------------------------------


def _conjuncts(expr: Any) -> list[Any]:
    """Flatten a top-level AND chain into its conjuncts (OR is never split)."""
    if isinstance(expr, BinaryExpr) and expr.op.upper() == "AND":
        return [*_conjuncts(expr.left), *_conjuncts(expr.right)]
    return [expr]


def render_predicates(predicates: list[Any]) -> str | None:
    if not predicates:
        return None
    return " AND ".join(format_expr(predicate) for predicate in predicates)


def _render_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def _expr_precedence(expr: Any) -> int:
    if isinstance(expr, BinaryExpr):
        op = expr.op.upper()
        if op in _PRECEDENCE:
            return _PRECEDENCE[op]
        return _ARITHMETIC_PRECEDENCE.get(op, _COMPARISON_PRECEDENCE)
    if isinstance(expr, UnaryExpr):
        return _UNARY_ARITHMETIC_PRECEDENCE if expr.op.upper() in {"+", "-"} else _NOT_PRECEDENCE
    if isinstance(expr, (IsNull, IsMissing, IsJsonNull, Like, InList, Between)):
        return _COMPARISON_PRECEDENCE
    return _PRIMARY_PRECEDENCE


def _wrap(inner: str, precedence: int, minimum: int) -> str:
    return f"({inner})" if precedence < minimum else inner


def format_expr(expr: Any) -> str:
    """Render an expression back to readable SQL for query plans."""
    if isinstance(expr, Literal):
        return _render_literal(expr.value)
    if isinstance(expr, Identifier):
        return expr.label
    if isinstance(expr, PathAccess):
        return expr.label
    if isinstance(expr, FunctionCall):
        args = ", ".join(format_expr(a) for a in expr.args)
        return f"{expr.func}({args})"
    if isinstance(expr, AggregateCall):
        if isinstance(expr.arg, Identifier) and expr.arg.name == "*":
            return f"{expr.func}(*)"
        return f"{expr.func}({format_expr(expr.arg)})"
    if isinstance(expr, UnaryExpr):
        precedence = _expr_precedence(expr)
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), precedence)
        return f"{expr.op.upper()} {inner}"
    if isinstance(expr, BinaryExpr):
        precedence = _expr_precedence(expr)
        left = _wrap(format_expr(expr.left), _expr_precedence(expr.left), precedence)
        right_precedence = precedence
        if expr.op.upper() in {"-", "/", "%"}:
            # Non-associative operators need their right operand parenthesized
            # even at equal precedence: ``a - (b - c)`` is not ``a - b - c``.
            right_precedence = precedence + 1
        right = _wrap(format_expr(expr.right), _expr_precedence(expr.right), right_precedence)
        return f"{left} {expr.op.upper()} {right}"
    if isinstance(expr, IsNull):
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), _COMPARISON_PRECEDENCE)
        return f"{inner} IS {'NOT ' if expr.negated else ''}NULL"
    if isinstance(expr, IsMissing):
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), _COMPARISON_PRECEDENCE)
        return f"{inner} IS {'NOT ' if expr.negated else ''}MISSING"
    if isinstance(expr, IsJsonNull):
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), _COMPARISON_PRECEDENCE)
        return f"{inner} IS {'NOT ' if expr.negated else ''}JSON NULL"
    if isinstance(expr, Like):
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), _COMPARISON_PRECEDENCE)
        pattern = _wrap(format_expr(expr.pattern), _expr_precedence(expr.pattern), _COMPARISON_PRECEDENCE)
        return f"{inner} {'NOT ' if expr.negated else ''}LIKE {pattern}"
    if isinstance(expr, InList):
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), _COMPARISON_PRECEDENCE)
        items = ", ".join(format_expr(item) for item in expr.items)
        return f"{inner} {'NOT ' if expr.negated else ''}IN ({items})"
    if isinstance(expr, Between):
        inner = _wrap(format_expr(expr.expr), _expr_precedence(expr.expr), _COMPARISON_PRECEDENCE)
        low = _wrap(format_expr(expr.low), _expr_precedence(expr.low), _COMPARISON_PRECEDENCE)
        high = _wrap(format_expr(expr.high), _expr_precedence(expr.high), _COMPARISON_PRECEDENCE)
        return f"{inner} {'NOT ' if expr.negated else ''}BETWEEN {low} AND {high}"
    if isinstance(expr, Case):
        parts = ["CASE"]
        if expr.base is not None:
            parts.append(format_expr(expr.base))
        for when in expr.whens:
            parts.append(f"WHEN {format_expr(when.condition)} THEN {format_expr(when.result)}")
        if expr.else_expr is not None:
            parts.append(f"ELSE {format_expr(expr.else_expr)}")
        parts.append("END")
        return " ".join(parts)
    if expr is None:
        return "NULL"
    return str(expr)


def _expr_sources(expr: Any, sources: list[SourceBinding]) -> set[str]:
    """Return the set of source refs an expression reads.

    Resolution failures are swallowed: planning must never replace the executor's
    canonical error message for an invalid column reference.
    """
    refs: set[str] = set()
    for node in walk_expr(expr):
        if isinstance(node, PathAccess):
            try:
                ref, _, _ = resolve_path_access(node, sources)
                refs.add(ref)
            except ExecutionError:
                pass
            continue
        if not isinstance(node, Identifier) or node.name == "*":
            continue
        try:
            ref, _ = resolve_identifier(node, sources)
        except ExecutionError:
            if node.qualifier is not None:
                col_matches = [s.ref for s in sources if node.qualifier in s.column_map]
                if len(col_matches) == 1:
                    refs.add(col_matches[0])
            continue
        refs.add(ref)
    return refs


def _identifier_for(expr: Any, ref: str) -> Identifier | None:
    if (
        isinstance(expr, Identifier)
        and expr.name != "*"
        and (expr.qualifier is None or expr.qualifier == ref)
    ):
        return expr
    return None


def _literal_value(expr: Any) -> tuple[bool, Any]:
    if isinstance(expr, Literal):
        return True, expr.value
    return False, None


def _range_fraction(column_stats: Any, op: str, value: Any) -> float | None:
    """Estimate the fraction of non-NULL rows a range predicate keeps.

    Returns ``None`` when the statistics cannot support the arithmetic (for
    example TEXT columns, where Python cannot measure a numeric span).
    """
    if column_stats is None or not getattr(column_stats, "min_max_available", False):
        return None
    low = column_stats.minimum
    high = column_stats.maximum
    try:
        span = high - low
    except TypeError:
        return None
    if span is None or span <= 0:
        return None
    try:
        if op in {">=", ">"}:
            fraction = (high - value) / span
        elif op in {"<=", "<"}:
            fraction = (value - low) / span
        else:
            return None
    except TypeError:
        return None
    return max(0.0, min(1.0, float(fraction)))


def _predicate_selectivity(
    predicate: Any,
    stats: TableStats | None,
    sources: list[SourceBinding],
) -> float:
    """A labelled estimate of the fraction of rows a predicate keeps.

    Only shapes the statistics actually describe get a refined estimate: a
    single-column comparison / ``IS NULL`` / ``IN`` / ``BETWEEN`` against literal
    values. Everything else (LIKE patterns, CASE conditions, arithmetic on
    columns) keeps the conservative default, because no statistics exist for
    them and inventing precision would be dishonest.
    """
    if isinstance(predicate, UnaryExpr):
        return DEFAULT_SELECTIVITY
    if isinstance(predicate, BinaryExpr) and predicate.op.upper() in {"AND", "OR"}:
        return DEFAULT_SELECTIVITY
    if isinstance(predicate, Like):
        return DEFAULT_SELECTIVITY
    if isinstance(predicate, Between):
        return _between_selectivity(predicate, stats, sources)
    if isinstance(predicate, InList):
        return _in_list_selectivity(predicate, stats, sources)
    if isinstance(predicate, IsNull):
        return _is_null_selectivity(predicate, stats, sources)
    if not isinstance(predicate, BinaryExpr):
        return DEFAULT_SELECTIVITY
    op = predicate.op.upper()
    if op not in {"=", "!=", "<>", "<", "<=", ">", ">="}:
        return DEFAULT_SELECTIVITY

    refs = _expr_sources(predicate, sources)
    if len(refs) != 1:
        return DEFAULT_SELECTIVITY
    ref = next(iter(refs))
    source = next((s for s in sources if s.ref == ref), None)
    if source is None:
        return DEFAULT_SELECTIVITY

    column = _identifier_for(predicate.left, ref) or _identifier_for(predicate.right, ref)
    literal_expr = predicate.right if column is predicate.left else predicate.left
    has_literal, value = _literal_value(literal_expr)
    if column is None or not has_literal:
        return DEFAULT_SELECTIVITY
    if value is None:
        # Comparisons against NULL are never TRUE, so no row can pass.
        return 0.0

    effective_op = op
    if column is predicate.right:
        effective_op = _FLIPPED_OPS.get(op, op)

    column_stats = stats.column(column.name) if stats is not None else None
    if effective_op == "=":
        distinct = getattr(column_stats, "distinct", 0) or 1
        return min(1.0, 1.0 / max(distinct, 1))
    if effective_op in {"!=", "<>"}:
        distinct = getattr(column_stats, "distinct", 0) or 1
        return max(0.0, 1.0 - 1.0 / max(distinct, 1))
    fraction = _range_fraction(column_stats, effective_op, value)
    if fraction is None:
        return DEFAULT_SELECTIVITY
    return fraction


def _single_column_of(expr: Any, sources: list[SourceBinding]) -> str | None:
    """Return the column name when ``expr`` is exactly one column of one source."""
    if not isinstance(expr, Identifier) or expr.name == "*":
        return None
    if len(_expr_sources(expr, sources)) != 1:
        return None
    try:
        _, col_def = resolve_identifier(expr, sources)
    except ExecutionError:
        return None
    return col_def.name


def _is_null_selectivity(
    predicate: IsNull,
    stats: TableStats | None,
    sources: list[SourceBinding],
) -> float:
    column_name = _single_column_of(predicate.expr, sources)
    if column_name is None or stats is None:
        return DEFAULT_SELECTIVITY
    column_stats = stats.column(column_name)
    if column_stats is None or stats.row_count <= 0:
        return DEFAULT_SELECTIVITY
    fraction = getattr(column_stats, "null_count", 0) / stats.row_count
    return min(1.0, max(0.0, fraction if not predicate.negated else 1.0 - fraction))


def _between_selectivity(
    predicate: Between,
    stats: TableStats | None,
    sources: list[SourceBinding],
) -> float:
    if predicate.negated:
        return DEFAULT_SELECTIVITY
    column_name = _single_column_of(predicate.expr, sources)
    if column_name is None or stats is None:
        return DEFAULT_SELECTIVITY
    low_ok, low = _literal_value(predicate.low)
    high_ok, high = _literal_value(predicate.high)
    if not (low_ok and high_ok) or low is None or high is None:
        return DEFAULT_SELECTIVITY
    column_stats = stats.column(column_name)
    lower = _range_fraction(column_stats, ">=", low)
    upper = _range_fraction(column_stats, "<=", high)
    if lower is None or upper is None:
        return DEFAULT_SELECTIVITY
    return min(1.0, max(0.0, lower + upper - 1.0))


def _in_list_selectivity(
    predicate: InList,
    stats: TableStats | None,
    sources: list[SourceBinding],
) -> float:
    column_name = _single_column_of(predicate.expr, sources)
    if column_name is None or stats is None:
        return DEFAULT_SELECTIVITY
    if not all(isinstance(item, Literal) for item in predicate.items):
        return DEFAULT_SELECTIVITY
    if any(item.value is None for item in predicate.items):
        # A NULL member can make the result UNKNOWN, never TRUE for that row;
        # the non-NULL members still select rows, so stay conservative.
        return DEFAULT_SELECTIVITY
    column_stats = stats.column(column_name)
    distinct = max(getattr(column_stats, "distinct", 0), 1)
    fraction = len({repr((type(i.value).__name__, i.value)) for i in predicate.items}) / distinct
    return min(1.0, max(0.0, fraction))


# --- index matching -------------------------------------------------------


def _literal_matches_column_type(value: Any, type_name: str) -> bool:
    normalized = type_name.upper()
    if normalized in {"INT", "INTEGER"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized in {"FLOAT", "DOUBLE"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized in {"TEXT", "VARCHAR"}:
        return isinstance(value, str)
    if normalized in {"BOOL", "BOOLEAN"}:
        return isinstance(value, bool) or (isinstance(value, int) and value in (0, 1))
    return False


def _indexed_candidates(db: Database, table_name: str, column: str) -> list[str]:
    return sorted(
        name
        for name, index in db.indexes.items()
        if index.table == table_name and index.column == column
    )


def match_indexed_predicate(
    db: Database,
    table_name: str,
    ref: str,
    predicate: Any,
) -> IndexMatch | None:
    """Match ``column <op> literal`` or ``column BETWEEN literal AND literal``.

    Only shapes that are provably equivalent to a contiguous B+ tree range are
    matched; everything else (``IS NULL``, ``LIKE``, arithmetic, ``NOT BETWEEN``)
    keeps a table scan.
    """
    if isinstance(predicate, Between):
        return _match_between(db, table_name, ref, predicate)
    if not isinstance(predicate, BinaryExpr):
        return None
    op = predicate.op.upper()
    if op not in _INDEX_OPS:
        return None

    column = None
    value = None
    if isinstance(predicate.left, Identifier) and isinstance(predicate.right, Literal):
        column = _identifier_for(predicate.left, ref)
        value = predicate.right.value
    elif isinstance(predicate.right, Identifier) and isinstance(predicate.left, Literal):
        column = _identifier_for(predicate.right, ref)
        op = _FLIPPED_OPS[op]
        value = predicate.left.value

    if column is None or value is None:
        return None

    table = db.table(table_name)
    column_def = next((col for col in table.columns if col.name == column.name), None)
    if column_def is None or not _literal_matches_column_type(value, column_def.type_name):
        return None

    candidates = _indexed_candidates(db, table_name, column.name)
    if not candidates:
        return None

    match = IndexMatch(index_name=candidates[0], column=column.name, predicate=predicate)
    if op == "=":
        match.start = value
        match.end = value
    elif op == ">":
        match.start = value
        match.start_inclusive = False
    elif op == ">=":
        match.start = value
    elif op == "<":
        match.end = value
        match.end_inclusive = False
    elif op == "<=":
        match.end = value
    return match


def _match_between(
    db: Database,
    table_name: str,
    ref: str,
    predicate: Between,
) -> IndexMatch | None:
    """``col BETWEEN low AND high`` over an index is one inclusive range."""
    if predicate.negated:
        return None
    column = _identifier_for(predicate.expr, ref)
    low_ok, low = _literal_value(predicate.low)
    high_ok, high = _literal_value(predicate.high)
    if column is None or not (low_ok and high_ok):
        return None
    if low is None or high is None:
        # A NULL bound makes the predicate UNKNOWN for every row: never an index
        # range, and never TRUE.
        return None

    table = db.table(table_name)
    column_def = next((col for col in table.columns if col.name == column.name), None)
    if column_def is None:
        return None
    if not (
        _literal_matches_column_type(low, column_def.type_name)
        and _literal_matches_column_type(high, column_def.type_name)
    ):
        return None
    candidates = _indexed_candidates(db, table_name, column.name)
    if not candidates:
        return None
    return IndexMatch(
        index_name=candidates[0],
        column=column.name,
        predicate=predicate,
        start=low,
        end=high,
    )


def _estimate_index_candidates(
    stats: TableStats,
    index_column: str,
    match: IndexMatch,
) -> float:
    """Estimate how many rows the index range returns, in rows (not exact)."""
    column_stats = stats.column(index_column)
    non_null = stats.row_count - getattr(column_stats, "null_count", 0)
    if non_null <= 0:
        return 0.0
    if match.start is not None and match.end is not None:
        if match.start == match.end:
            distinct = max(getattr(column_stats, "distinct", 0), 1)
            return max(1.0, non_null / distinct)
        lower = _range_fraction(column_stats, ">=" if match.start_inclusive else ">", match.start)
        upper = _range_fraction(column_stats, "<=" if match.end_inclusive else "<", match.end)
        if lower is None or upper is None:
            return max(1.0, non_null / 3.0)
        return max(0.0, non_null * max(0.0, lower + upper - 1.0))
    if match.start is not None and match.end is None:
        fraction = _range_fraction(column_stats, ">=" if match.start_inclusive else ">", match.start)
    elif match.end is not None and match.start is None:
        fraction = _range_fraction(column_stats, "<=" if match.end_inclusive else "<", match.end)
    else:
        fraction = None
    if fraction is None:
        return max(1.0, non_null / 3.0)
    return max(0.0, non_null * fraction)


def _estimate_rows_after(predicates: list[Any], rows: float, stats: TableStats | None, sources: list[SourceBinding]) -> float:
    estimate = float(rows)
    for predicate in predicates:
        estimate *= _predicate_selectivity(predicate, stats, sources)
    return max(0.0, estimate)


# --- logical planning -----------------------------------------------------


def _projection_text(stmt: Select) -> str:
    if (
        len(stmt.columns) == 1
        and isinstance(stmt.columns[0].expr, Identifier)
        and stmt.columns[0].expr.name == "*"
        and stmt.columns[0].expr.qualifier is None
    ):
        return "*"
    labels = []
    for index, item in enumerate(stmt.columns):
        expr = item.expr
        computed = not isinstance(expr, (Identifier, AggregateCall))
        if not item.alias:
            if isinstance(expr, Identifier):
                label = expr.label
            elif isinstance(expr, AggregateCall):
                label = format_expr(expr)
            else:
                label = f"expr_{index + 1}: {format_expr(expr)}"
        elif computed:
            # Keep the computed expression visible beside its alias, otherwise
            # EXPLAIN would hide the whole arithmetic / CASE expression.
            label = f"{item.alias}: {format_expr(expr)}"
        else:
            label = item.alias
        labels.append(label)
    return ", ".join(labels)


def _sort_text(stmt: Select) -> str | None:
    if not stmt.order_by:
        return None
    return ", ".join(f"{format_expr(expr)} {'DESC' if desc else 'ASC'}" for expr, desc in stmt.order_by)


def build_logical_plan(stmt: Select, sources: list[SourceBinding]) -> LogicalPlan:
    """Bind sources and place every predicate, without choosing an algorithm."""
    nullable_refs: set[str] = set()
    joins: list[JoinSpec] = []
    left_refs = [sources[0].ref]
    for index, join in enumerate(stmt.joins):
        join_ref = sources[index + 1].ref
        joins.append(
            JoinSpec(
                join_type=join.join_type.upper(),
                condition=join.condition,
                right_ref=join_ref,
                right_index=index + 1,
                left_refs=list(left_refs),
            )
        )
        if join.join_type.upper() == "LEFT":
            # The right side of a LEFT JOIN is null-extended: predicates over it
            # must not be evaluated before the join produces those rows.
            nullable_refs.add(join_ref)
        left_refs.append(join_ref)

    pushed: dict[str, list[Any]] = {source.ref: [] for source in sources}
    on_predicates: dict[int, list[Any]] = {index: [] for index in range(len(joins))}
    for index, spec in enumerate(joins):
        for conjunct in _conjuncts(spec.condition):
            refs = _expr_sources(conjunct, sources)
            if refs == {spec.right_ref}:
                # Safe for INNER and for LEFT: a right row that fails an ON
                # conjunct can never match any left row, so removing it before
                # the join only turns those left rows into null-extended rows,
                # which is exactly what the join would have produced anyway.
                pushed[spec.right_ref].append(conjunct)
                on_predicates[index].append(conjunct)
            elif len(refs) == 1 and spec.join_type != "LEFT" and refs <= set(spec.left_refs):
                # INNER JOIN only: a left-side ON conjunct is a row filter on
                # that input. For LEFT JOIN it is never pushed below the join.
                ref = next(iter(refs))
                pushed[ref].append(conjunct)
                on_predicates[index].append(conjunct)

    post_filters: list[Any] = []
    if stmt.where is not None:
        for conjunct in _conjuncts(stmt.where):
            refs = _expr_sources(conjunct, sources)
            target = next(iter(refs)) if len(refs) == 1 else None
            if target is not None and target not in nullable_refs:
                pushed[target].append(conjunct)
            else:
                # Multi-relation predicates and predicates over a null-extended
                # side stay after the joins, where the original WHERE applied.
                post_filters.append(conjunct)

    has_group_by = bool(stmt.group_by)
    is_grouped = has_group_by or stmt.having is not None or any(
        contains_aggregate(item.expr) for item in stmt.columns
    )
    group_text = None
    if has_group_by:
        group_text = "GROUP BY " + ", ".join(format_expr(ident) for ident in stmt.group_by)
    elif is_grouped:
        group_text = "aggregate"

    return LogicalPlan(
        stmt=stmt,
        sources=sources,
        joins=joins,
        pushed=pushed,
        post_filters=post_filters,
        is_grouped=is_grouped,
        has_group_by=has_group_by,
        has_order_by=bool(stmt.order_by),
        limit=stmt.limit,
        distinct=stmt.distinct,
        offset=stmt.offset,
        on_predicates=on_predicates,
        projection_text=_projection_text(stmt),
        sort_text=_sort_text(stmt),
        group_text=group_text,
        nullable_refs=nullable_refs,
    )


# --- physical planning ----------------------------------------------------


def _index_seek_cost(table_rows: float) -> float:
    """B+ tree descent cost: one fixed setup plus one step per tree level."""
    return COST_INDEX_SEEK + COST_INDEX_LEVEL * math.log2(table_rows + 1.0)


def _access_cost(candidates: float, table_rows: float) -> tuple[float, float]:
    """Cost of a table scan and of an index scan for the same predicate."""
    table_cost = table_rows * COST_SEQ_ROW
    index_cost = (
        _index_seek_cost(table_rows)
        + candidates * COST_INDEX_ROW
        + candidates * math.log2(candidates + 1.0) * COST_INDEX_SORT
    )
    return table_cost, index_cost


def _build_scan(
    db: Database,
    source: SourceBinding,
    predicates: list[Any],
    sources: list[SourceBinding],
    *,
    force_access_path: str | None,
) -> PlanNode:
    table = db.table(source.table.name)
    stats = db.stats.get(table)
    match: IndexMatch | None = None
    candidate_estimate = 0.0
    for predicate in predicates:
        try:
            match = match_indexed_predicate(db, table.name, source.ref, predicate)
        except ExecutionError:
            match = None
        if match is not None:
            candidate_estimate = _estimate_index_candidates(stats, match.column, match)
            break

    use_index = False
    why: str | None = None
    if match is not None and force_access_path != "TABLE":
        table_cost, index_cost = _access_cost(candidate_estimate, stats.row_count)
        use_index = index_cost < table_cost
        if force_access_path == "INDEX":
            use_index = True
            why = "index scan forced for verification"
        elif use_index:
            why = (
                f"index estimated cheaper: ~{index_cost:.1f} cost units vs "
                f"~{table_cost:.1f} for a table scan (~{candidate_estimate:.0f} candidates)"
            )
        else:
            why = (
                f"table scan estimated cheaper: ~{table_cost:.1f} cost units vs "
                f"~{index_cost:.1f} for {match.index_name} (~{candidate_estimate:.0f} candidates)"
            )
    elif force_access_path == "INDEX" and match is None:
        why = "no index matches this predicate; using a table scan"

    if use_index and match is not None:
        # The index range already applied its own predicate: applying its
        # selectivity again would double count.
        residual = [p for p in predicates if p is not match.predicate]
        scan_rows = _estimate_rows_after(residual, candidate_estimate, stats, sources)
    else:
        scan_rows = _estimate_rows_after(predicates, float(stats.row_count), stats, sources)
    read_rows = candidate_estimate if use_index else float(stats.row_count)
    table_cost, index_cost = _access_cost(candidate_estimate, stats.row_count)
    cost = index_cost if use_index else table_cost

    node = PlanNode(
        op="INDEX SCAN" if use_index else "TABLE SCAN",
        table=table.name,
        ref=source.ref,
        index_name=match.index_name if use_index and match is not None else None,
        index_column=match.column if use_index and match is not None else None,
        condition=format_expr(match.predicate) if use_index and match is not None else None,
        start=match.start if use_index and match is not None else None,
        end=match.end if use_index and match is not None else None,
        start_inclusive=match.start_inclusive if use_index and match is not None else True,
        end_inclusive=match.end_inclusive if use_index and match is not None else True,
        predicates=list(predicates),
        predicate_text=render_predicates(predicates),
        estimated_rows=scan_rows,
        estimated_cost=cost,
        why=why,
    )
    # Planning-time estimates only; measured counters are added during execution.
    node.actual["estimated_rows_read"] = read_rows
    return node


def _equality_pairs(
    condition: Any,
    left_refs: list[str],
    right_ref: str,
    sources: list[SourceBinding],
) -> list[tuple[str, str, str, str]]:
    """Extract hashable ``left.col = right.col`` conjuncts from an ON clause."""
    pairs: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for conjunct in _conjuncts(condition):
        if not isinstance(conjunct, BinaryExpr) or conjunct.op.upper() != "=":
            continue
        left, right = conjunct.left, conjunct.right
        if not isinstance(left, Identifier) or not isinstance(right, Identifier):
            continue
        if left.name == "*" or right.name == "*":
            continue
        try:
            left_ref, left_col = resolve_identifier(left, sources)
            right_ref_of, right_col = resolve_identifier(right, sources)
        except ExecutionError:
            continue
        if left_ref in left_refs and right_ref_of == right_ref:
            pair = (left_ref, left_col.name, right_ref, right_col.name)
        elif right_ref_of in left_refs and left_ref == right_ref:
            pair = (right_ref_of, right_col.name, right_ref, left_col.name)
        else:
            continue
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _estimate_join_rows(
    left_rows: float,
    right_rows: float,
    pairs: list[tuple[str, str, str, str]],
    left_stats: dict[str, TableStats],
    right_stats: TableStats,
) -> float:
    if not pairs:
        # Without an equality key the only honest estimate is the cross product.
        return left_rows * right_rows
    denominator = 1.0
    for left_ref, left_column, _, right_column in pairs:
        table_stats = left_stats.get(left_ref)
        left_distinct = 1
        if table_stats is not None:
            column_stats = table_stats.column(left_column)
            left_distinct = max(getattr(column_stats, "distinct", 0), 1)
        column_stats = right_stats.column(right_column)
        right_distinct = max(getattr(column_stats, "distinct", 0), 1)
        denominator *= max(left_distinct, right_distinct)
    return max(0.0, (left_rows * right_rows) / denominator)


def _build_join(
    db: Database,
    logical: LogicalPlan,
    left: PlanNode,
    spec: JoinSpec,
    right_source: SourceBinding,
    right: PlanNode,
    left_stats: dict[str, TableStats],
    right_stats: TableStats,
    *,
    force_join_algorithm: str | None,
) -> PlanNode:
    pairs = _equality_pairs(spec.condition, spec.left_refs, spec.right_ref, logical.sources)
    left_rows = left.estimated_rows
    right_rows = right.estimated_rows
    inner_rows = _estimate_join_rows(left_rows, right_rows, pairs, left_stats, right_stats)
    estimated_rows = max(inner_rows, left_rows) if spec.join_type == "LEFT" else inner_rows

    cost_left = left.estimated_cost
    cost_right = right.estimated_cost
    nl_cost = left_rows * right_rows * COST_NL_COMPARE
    hash_cost = right_rows * COST_HASH_BUILD + left_rows * COST_HASH_PROBE + inner_rows * COST_HASH_MATCH

    use_hash = False
    why = None
    if force_join_algorithm in {"HASH", "NESTED LOOP"}:
        use_hash = force_join_algorithm == "HASH" and bool(pairs)
        why = f"join algorithm forced to {'hash' if use_hash else 'nested loop'} for verification"
        if force_join_algorithm == "HASH" and not pairs:
            why = "hash join forced but no equality key is available; using nested loop"
    elif not pairs:
        why = "no hashable equality key; using nested loop"
    elif min(left_rows, right_rows) < HASH_MIN_INPUT_ROWS:
        why = (
            f"inputs are small (~{left_rows:.0f} x ~{right_rows:.0f}); "
            "nested loop avoids hash-table setup"
        )
    else:
        use_hash = hash_cost < nl_cost
        why = (
            f"{'hash' if use_hash else 'nested loop'} join estimated cheaper: "
            f"~{hash_cost if use_hash else nl_cost:.1f} vs "
            f"~{nl_cost if use_hash else hash_cost:.1f} cost units "
            f"(build ~{right_rows:.0f}, probe ~{left_rows:.0f})"
        )

    node = PlanNode(
        op="JOIN",
        children=[left, right],
        estimated_rows=estimated_rows,
        estimated_cost=(hash_cost if use_hash else nl_cost) + cost_left + cost_right,
        join_type=spec.join_type,
        algorithm="HASH" if use_hash else "NESTED LOOP",
        on=spec.condition,
        on_text=format_expr(spec.condition),
        key_pairs=pairs if use_hash else [],
        why=why,
    )
    node.actual["left_rows"] = left_rows
    node.actual["right_rows"] = right_rows
    return node


def physical_plan(
    db: Database,
    logical: LogicalPlan,
    *,
    force_join_algorithm: str | None = None,
    force_access_path: str | None = None,
) -> PlanNode:
    """Choose access paths and join algorithms for a logical plan."""
    stats = {source.ref: db.stats.get(db.table(source.table.name)) for source in logical.sources}
    nodes: dict[str, PlanNode] = {}
    for source in logical.sources:
        nodes[source.ref] = _build_scan(
            db,
            source,
            logical.pushed.get(source.ref, []),
            logical.sources,
            force_access_path=force_access_path,
        )

    root = nodes[logical.sources[0].ref]
    for index, spec in enumerate(logical.joins):
        right_source = logical.sources[spec.right_index]
        root = _build_join(
            db,
            logical,
            root,
            spec,
            right_source,
            nodes[spec.right_ref],
            stats,
            stats[spec.right_ref],
            force_join_algorithm=force_join_algorithm,
        )

    if logical.post_filters:
        rows = _estimate_rows_after(logical.post_filters, root.estimated_rows, None, logical.sources)
        root = PlanNode(
            op="FILTER",
            children=[root],
            estimated_rows=rows,
            estimated_cost=root.estimated_cost + root.estimated_rows,
            predicates=list(logical.post_filters),
            predicate_text=render_predicates(logical.post_filters),
        )

    if logical.is_grouped:
        input_rows = root.estimated_rows
        if logical.has_group_by:
            groups = 1.0
            for ident in logical.stmt.group_by:
                try:
                    ref, col_def = resolve_identifier(ident, logical.sources)
                except ExecutionError:
                    groups = max(1.0, input_rows / 10.0)
                    break
                table_stats = stats.get(ref)
                column_stats = table_stats.column(col_def.name) if table_stats is not None else None
                groups *= max(getattr(column_stats, "distinct", 0), 1)
            group_rows = max(1.0, min(input_rows, groups))
        else:
            group_rows = 1.0
        root = PlanNode(
            op="AGGREGATE",
            children=[root],
            estimated_rows=group_rows,
            estimated_cost=root.estimated_cost + input_rows,
            detail=logical.group_text,
        )

    # A pure column projection over one table stays elided; a computed
    # projection (arithmetic, CASE, a predicate value) is a real operator the
    # plan must show, so its expression text is readable in EXPLAIN.
    trivial_projection = all(
        isinstance(item.expr, Identifier) for item in logical.stmt.columns
    )
    root = PlanNode(
        op="PROJECTION",
        children=[root],
        estimated_rows=root.estimated_rows,
        estimated_cost=root.estimated_cost + root.estimated_rows,
        detail=logical.projection_text,
        elided=not logical.joins and not logical.is_grouped and trivial_projection,
    )

    if logical.distinct:
        rows = root.estimated_rows
        root = PlanNode(
            op="DISTINCT",
            children=[root],
            estimated_rows=rows,
            estimated_cost=root.estimated_cost + rows,
            detail=logical.projection_text,
        )

    if logical.has_order_by:
        rows = root.estimated_rows
        root = PlanNode(
            op="SORT",
            children=[root],
            estimated_rows=rows,
            estimated_cost=root.estimated_cost + rows * math.log2(rows + 1.0) * COST_HASH_MATCH * 2,
            detail=logical.sort_text,
        )

    if logical.offset:
        rows = max(0.0, root.estimated_rows - float(logical.offset))
        root = PlanNode(
            op="OFFSET",
            children=[root],
            estimated_rows=rows,
            estimated_cost=root.estimated_cost + float(logical.offset),
            detail=str(logical.offset),
        )

    if logical.limit is not None:
        rows = min(root.estimated_rows, float(logical.limit))
        root = PlanNode(
            op="LIMIT",
            children=[root],
            estimated_rows=rows,
            estimated_cost=root.estimated_cost,
            detail=str(logical.limit),
        )

    return root


# --- entry points ---------------------------------------------------------


def plan_select(
    db: Database,
    stmt: Select,
    *,
    force_join_algorithm: str | None = None,
    force_access_path: str | None = None,
) -> QueryPlan:
    """Build the logical plan and choose the physical plan the executor runs."""
    base_ref = stmt.table_alias or stmt.table
    sources: list[SourceBinding] = [SourceBinding.from_table(base_ref, db.table(stmt.table))]
    for join in stmt.joins:
        join_ref = join.alias or join.table
        sources.append(SourceBinding.from_table(join_ref, db.table(join.table)))

    logical = build_logical_plan(stmt, sources)
    root = physical_plan(
        db,
        logical,
        force_join_algorithm=force_join_algorithm,
        force_access_path=force_access_path,
    )

    scan = _base_scan(root)
    filter_text = render_predicates(logical.post_filters)
    if filter_text is None and scan is not None:
        filter_text = scan.predicate_text
    return QueryPlan(
        kind=scan.op if scan is not None else "TABLE SCAN",
        table=stmt.table,
        index_name=scan.index_name if scan is not None else None,
        index_column=scan.index_column if scan is not None else None,
        condition=scan.condition if scan is not None else None,
        filter=filter_text,
        start=scan.start if scan is not None else None,
        end=scan.end if scan is not None else None,
        start_inclusive=scan.start_inclusive if scan is not None else True,
        end_inclusive=scan.end_inclusive if scan is not None else True,
        joins=len(stmt.joins),
        root=root,
    )


def _base_scan(node: PlanNode | None) -> PlanNode | None:
    """Return the leftmost base scan of a plan tree."""
    while node is not None:
        if node.op in ("TABLE SCAN", "INDEX SCAN"):
            return node
        if not node.children:
            return None
        node = node.children[0]
    return None


def fetch_plan_rows(db: Database, plan: QueryPlan) -> list[dict[str, Any]]:
    """Retrieve candidate rows for a plan's base access path, in table order."""
    if plan.index_name is None:
        return list(db.table(plan.table).rows)
    index = db.indexes.get(plan.index_name)
    if index is None:
        raise ExecutionError(
            f"Index '{plan.index_name}' does not exist; cannot execute the planned index scan"
        )
    return index.range(
        start=plan.start,
        end=plan.end,
        start_inclusive=plan.start_inclusive,
        end_inclusive=plan.end_inclusive,
    )


# --- textual rendering ----------------------------------------------------


def _render_node(node: PlanNode, depth: int) -> list[str]:
    indent = "  " * depth
    if node.elided and len(node.children) == 1:
        return _render_node(node.children[0], depth)

    lines: list[str] = []
    if node.op == "TABLE SCAN":
        lines.append(f"{indent}TABLE SCAN: {node.table}")
        if node.predicate_text:
            lines.append(f"{indent}  FILTER: {node.predicate_text}")
        if node.why:
            lines.append(f"{indent}  WHY: {node.why}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
        return lines
    if node.op == "INDEX SCAN":
        lines.append(f"{indent}INDEX SCAN: {node.index_name}")
        lines.append(f"{indent}  TABLE: {node.table}")
        if node.condition:
            lines.append(f"{indent}  CONDITION: {node.condition}")
        if node.predicate_text:
            lines.append(f"{indent}  FILTER: {node.predicate_text}")
        if node.why:
            lines.append(f"{indent}  WHY: {node.why}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
        return lines
    if node.op == "JOIN":
        lines.append(f"{indent}JOIN: {node.algorithm} ({node.join_type})")
        if node.on_text:
            lines.append(f"{indent}  ON: {node.on_text}")
        if node.key_pairs:
            keys = ", ".join(
                f"{left_ref}.{left_col}={right_ref}.{right_col}"
                for left_ref, left_col, right_ref, right_col in node.key_pairs
            )
            lines.append(f"{indent}  HASH KEYS: {keys}")
        if node.why:
            lines.append(f"{indent}  WHY: {node.why}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
        for child in node.children:
            lines.extend(_render_node(child, depth + 1))
        return lines
    if node.op == "FILTER":
        lines.append(f"{indent}FILTER: {node.predicate_text}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
    elif node.op == "AGGREGATE":
        label = node.detail or "aggregate"
        lines.append(f"{indent}AGGREGATE: {label}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
    elif node.op == "PROJECTION":
        lines.append(f"{indent}PROJECTION: {node.detail}")
    elif node.op == "SORT":
        lines.append(f"{indent}SORT: {node.detail}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
    elif node.op == "DISTINCT":
        lines.append(f"{indent}DISTINCT: {node.detail or 'all selected columns'}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f} (upper bound)")
    elif node.op == "OFFSET":
        lines.append(f"{indent}OFFSET: {node.detail}")
        lines.append(f"{indent}  ESTIMATED ROWS: ~{node.estimated_rows:.0f}")
    elif node.op == "LIMIT":
        lines.append(f"{indent}LIMIT: {node.detail}")
    for child in node.children:
        lines.extend(_render_node(child, depth + 1))
    return lines


def format_plan(plan: QueryPlan, include_note: bool = True) -> str:
    note = f"\n\n{ESTIMATE_NOTE}" if include_note else ""
    if plan.root is None:
        # Defensive fallback for a hand-built QueryPlan (kept for compatibility).
        lines = ["QUERY PLAN", ""]
        if plan.index_name is not None:
            lines.append(f"INDEX SCAN: {plan.index_name}")
            lines.append(f"  TABLE: {plan.table}")
            if plan.condition is not None:
                lines.append(f"  CONDITION: {plan.condition}")
        else:
            lines.append(f"TABLE SCAN: {plan.table}")
        if plan.filter is not None:
            lines.append(f"  FILTER: {plan.filter}")
        return "\n".join(lines) + note

    lines = ["QUERY PLAN", "", *_render_node(plan.root, 0)]
    return "\n".join(lines) + note


def format_metrics(metrics: dict[str, Any]) -> str:
    lines = []
    if metrics.get("join_algorithm"):
        lines.append(f"  JOIN ALGORITHM: {metrics['join_algorithm']}")
    if metrics.get("index"):
        lines.append(f"  INDEX USED: {metrics['index']}")
    lines.append(f"  ROWS SCANNED: {metrics['rows_scanned']}")
    lines.append(f"  ROWS FILTERED: {metrics['rows_filtered']}")
    lines.append(f"  ROWS RETURNED: {metrics['rows_produced']}")
    lines.append(f"  EXECUTION TIME: {metrics['execution_ms']:.3f} ms")
    operator_lines = metrics.get("operator_lines")
    if operator_lines:
        lines.append("  OPERATORS:")
        lines.extend(f"    {line}" for line in operator_lines)
    return "\n".join(lines)
