"""Query planning for single-table index access.

The planner only reports the plan the executor actually runs: either scanning the
table or scanning one B+ tree index. It never rewrites predicates, so the original
WHERE clause is always re-applied after candidate retrieval.

Unbounded range sides are represented by ``None``; a NULL predicate value is never
planned as an index bound because comparisons against NULL are never TRUE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ast import (
    AggregateCall,
    BinaryExpr,
    Identifier,
    Literal,
    Select,
    UnaryExpr,
)
from .errors import ExecutionError
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

_PRECEDENCE = {"OR": 1, "AND": 2}
_COMPARISON_PRECEDENCE = 3
_NOT_PRECEDENCE = 4


@dataclass(frozen=True)
class QueryPlan:
    """The plan selected for one SELECT statement."""

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


def _conjuncts(expr: Any) -> list[Any]:
    """Flatten a top-level AND chain into its conjuncts (OR is never split)."""
    if isinstance(expr, BinaryExpr) and expr.op.upper() == "AND":
        return [*_conjuncts(expr.left), *_conjuncts(expr.right)]
    return [expr]


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


def _column_reference(expr: Any, base_ref: str) -> str | None:
    """Return the referenced column name when ``expr`` targets the base source."""
    if not isinstance(expr, Identifier) or expr.name == "*":
        return None
    if expr.qualifier is not None and expr.qualifier != base_ref:
        return None
    return expr.name


def _match_indexed_predicate(db: Database, stmt: Select, predicate: Any) -> IndexMatch | None:
    """Match ``column <op> literal`` against an existing single-column index."""
    if not isinstance(predicate, BinaryExpr):
        return None
    op = predicate.op.upper()
    if op not in _INDEX_OPS:
        return None

    base_ref = stmt.table_alias or stmt.table
    column = None
    value = None
    if isinstance(predicate.left, Identifier) and isinstance(predicate.right, Literal):
        column = _column_reference(predicate.left, base_ref)
        value = predicate.right.value
    elif isinstance(predicate.right, Identifier) and isinstance(predicate.left, Literal):
        column = _column_reference(predicate.right, base_ref)
        op = _FLIPPED_OPS[op]
        value = predicate.left.value

    if column is None or value is None:
        return None

    table = db.table(stmt.table)
    column_def = next((col for col in table.columns if col.name == column), None)
    if column_def is None or not _literal_matches_column_type(value, column_def.type_name):
        return None

    candidates = _indexed_candidates(db, stmt.table, column)
    if not candidates:
        return None

    match = IndexMatch(index_name=candidates[0], column=column, predicate=predicate)
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


def plan_select(db: Database, stmt: Select) -> QueryPlan:
    """Choose between an index scan and a table scan for a SELECT statement."""
    filter_text = format_expr(stmt.where) if stmt.where is not None else None

    if stmt.joins or stmt.where is None:
        return QueryPlan(
            kind="TABLE SCAN",
            table=stmt.table,
            filter=filter_text,
            joins=len(stmt.joins),
        )

    for conjunct in _conjuncts(stmt.where):
        try:
            match = _match_indexed_predicate(db, stmt, conjunct)
        except ExecutionError:
            match = None
        if match is None:
            continue
        return QueryPlan(
            kind="INDEX SCAN",
            table=stmt.table,
            index_name=match.index_name,
            index_column=match.column,
            condition=format_expr(match.predicate),
            filter=filter_text,
            start=match.start,
            end=match.end,
            start_inclusive=match.start_inclusive,
            end_inclusive=match.end_inclusive,
        )

    return QueryPlan(kind="TABLE SCAN", table=stmt.table, filter=filter_text)


def fetch_plan_rows(db: Database, plan: QueryPlan) -> list[dict[str, Any]]:
    """Retrieve candidate rows for a plan, in index order when an index is used."""
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


# --- textual rendering ---


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
        return _COMPARISON_PRECEDENCE
    if isinstance(expr, UnaryExpr):
        return _NOT_PRECEDENCE
    return _NOT_PRECEDENCE + 1


def format_expr(expr: Any) -> str:
    """Render an expression back to readable SQL for query plans."""
    if isinstance(expr, Literal):
        return _render_literal(expr.value)
    if isinstance(expr, Identifier):
        return expr.label
    if isinstance(expr, AggregateCall):
        return expr.label
    if isinstance(expr, UnaryExpr):
        inner = format_expr(expr.expr)
        if _expr_precedence(expr.expr) < _NOT_PRECEDENCE:
            inner = f"({inner})"
        return f"{expr.op.upper()} {inner}"
    if isinstance(expr, BinaryExpr):
        precedence = _expr_precedence(expr)
        left = format_expr(expr.left)
        right = format_expr(expr.right)
        if _expr_precedence(expr.left) < precedence:
            left = f"({left})"
        if _expr_precedence(expr.right) < precedence:
            right = f"({right})"
        return f"{left} {expr.op.upper()} {right}"
    if expr is None:
        return "NULL"
    return str(expr)


def format_plan(plan: QueryPlan) -> str:
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
    if plan.joins:
        lines.append(f"  JOIN: NESTED LOOP ({plan.joins})")
    return "\n".join(lines)


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
    return "\n".join(lines)
