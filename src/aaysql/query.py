"""Expression validation and SELECT entry point.

The runtime semantics live in :mod:`aaysql.expressions`; this module keeps the
statement-level validation helpers (column resolution, aggregate placement,
grouped-expression rules, output labels) and the ``execute_select`` entry point.
Everything here walks expressions through the shared traversal in
:mod:`aaysql.expressions`, so a new node type is validated automatically.
"""

from __future__ import annotations

from typing import Any

from .ast import AggregateCall, Identifier, Literal, Select, SelectItem
from .errors import ExecutionError
from .expressions import (  # re-exported: the canonical implementation
    COMPARISON_OPS,
    EXPRESSION_TYPES,
    NUMERIC_TYPES,
    SourceBinding,
    compare_values,
    contains_aggregate,
    ensure_known_expr,
    eval_expr,
    eval_row_expr,
    is_sql_true,
    iter_expr_children,
    resolve_identifier,
    sql_and,
    sql_not,
    sql_or,
    to_logic,
    walk_expr,
)
from .storage import Database

_NUMERIC_TYPES = NUMERIC_TYPES


def _validate_no_aggregates(expr: Any, context_name: str) -> None:
    ensure_known_expr(expr)
    if contains_aggregate(expr):
        raise ExecutionError(f"Aggregate functions are not allowed in {context_name}")


def _validate_aggregate_arg(agg: AggregateCall, sources: list[SourceBinding]) -> None:
    if contains_aggregate(agg.arg):
        raise ExecutionError("Nested aggregate functions are not allowed")
    if agg.func == "COUNT" and isinstance(agg.arg, Identifier) and agg.arg.name == "*" and agg.arg.qualifier is None:
        return
    validate_expr_columns(agg.arg, sources, allow_aggregates=False, context_name=f"{agg.func}()")
    if agg.func in {"SUM", "AVG"}:
        if isinstance(agg.arg, Identifier):
            _, col_def = resolve_identifier(agg.arg, sources)
            if col_def.type_name.upper() not in _NUMERIC_TYPES:
                raise ExecutionError(
                    f"{agg.func}() requires a numeric column, got {col_def.type_name} for '{agg.arg.label}'"
                )
        elif isinstance(agg.arg, Literal):
            val = agg.arg.value
            if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float))):
                raise ExecutionError(f"{agg.func}() requires numeric values, got {type(val).__name__}")


def validate_expr_columns(
    expr: Any,
    sources: list[SourceBinding],
    *,
    allow_aggregates: bool = False,
    context_name: str = "expression",
) -> None:
    """Validate all column references and aggregate placement in an expression."""
    ensure_known_expr(expr)
    for node in walk_expr(expr):
        if isinstance(node, Identifier):
            resolve_identifier(node, sources)
        elif isinstance(node, AggregateCall):
            if not allow_aggregates:
                raise ExecutionError(f"Aggregate functions are not allowed in {context_name}")
            _validate_aggregate_arg(node, sources)


def _validate_grouped_expr(
    expr: Any,
    sources: list[SourceBinding],
    grouped_keys: set[tuple[str, str]],
) -> None:
    """Ensure every non-aggregated column reference in expr belongs to GROUP BY."""
    if expr is None or isinstance(expr, (Literal, Identifier, str, int, float, bool)):
        if isinstance(expr, Identifier):
            ref, col_def = resolve_identifier(expr, sources)
            if (ref, col_def.name) not in grouped_keys:
                raise ExecutionError(
                    f"Column '{expr.label}' must appear in GROUP BY clause or be used in an aggregate function"
                )
        return
    if isinstance(expr, AggregateCall):
        _validate_aggregate_arg(expr, sources)
        return
    ensure_known_expr(expr)
    for child in iter_expr_children(expr):
        _validate_grouped_expr(child, sources, grouped_keys)


def _eval_aggregate(
    agg: AggregateCall,
    group_rows: list[dict[str, dict[str, Any]]],
    sources: list[SourceBinding],
) -> Any:
    func = agg.func
    if func == "COUNT" and isinstance(agg.arg, Identifier) and agg.arg.name == "*" and agg.arg.qualifier is None:
        return len(group_rows)

    values = [eval_row_expr(agg.arg, ctx, sources) for ctx in group_rows]
    non_null = [v for v in values if v is not None]

    if func == "COUNT":
        return len(non_null)
    if func in {"SUM", "AVG"}:
        for v in non_null:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ExecutionError(f"{func}() requires numeric values, got {type(v).__name__}")
        if not non_null:
            return None
        total = sum(non_null)
        return total if func == "SUM" else total / len(non_null)
    if func == "MIN":
        if not non_null:
            return None
        try:
            return min(non_null)
        except TypeError as exc:
            raise ExecutionError("MIN() encountered incompatible value types") from exc
    if func == "MAX":
        if not non_null:
            return None
        try:
            return max(non_null)
        except TypeError as exc:
            raise ExecutionError("MAX() encountered incompatible value types") from exc
    raise ExecutionError(f"Unsupported aggregate function '{func}'")


def _eval_group_expr(
    expr: Any,
    sample_ctx: dict[str, dict[str, Any]] | None,
    group_rows: list[dict[str, dict[str, Any]]],
    sources: list[SourceBinding],
) -> Any:
    """Evaluate a group-level expression, resolving aggregate calls against the group."""
    return eval_expr(
        expr,
        sample_ctx,
        sources,
        aggregate_eval=lambda agg: _eval_aggregate(agg, group_rows, sources),
    )


def _default_expr_label(expr: Any, index: int) -> str:
    if isinstance(expr, Identifier):
        return expr.label
    if isinstance(expr, AggregateCall):
        return expr.label
    return f"expr_{index + 1}"


def _is_wildcard_select(columns: list[SelectItem]) -> bool:
    return (
        len(columns) == 1
        and isinstance(columns[0].expr, Identifier)
        and columns[0].expr.name == "*"
        and columns[0].expr.qualifier is None
    )


def _normalize_order_expr(expr: Any) -> Any:
    if isinstance(expr, str):
        return Identifier(expr)
    return expr


def _validate_output_labels(columns: list[str]) -> None:
    """Reject duplicate output labels instead of silently overwriting a value.

    Result rows are keyed by output label, so two selected expressions sharing a
    label (``u.id AS identifier, o.id AS identifier`` or ``u.id, u.id``) would
    collide. Distinct aliases are required until positional results are added.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in columns:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        listed = ", ".join(f"'{name}'" for name in duplicates)
        raise ExecutionError(
            f"Duplicate output column name {listed} in SELECT list; "
            f"use a distinct alias for each selected expression"
        )


def execute_select(
    db: Database,
    stmt: Select,
    plan: Any | None = None,
    collect_metrics: bool = False,
    **options: Any,
) -> dict[str, Any]:
    """Execute a SELECT through the physical plan chosen for it.

    The implementation lives in :mod:`aaysql.execution`. It is imported lazily so
    that the expression and three-valued-logic helpers defined here stay free of
    any planning dependency.
    """
    from .execution import execute_select as _execute_select

    return _execute_select(
        db,
        stmt,
        plan=plan,
        collect_metrics=collect_metrics,
        **options,
    )


__all__ = [
    "COMPARISON_OPS",
    "EXPRESSION_TYPES",
    "SourceBinding",
    "compare_values",
    "contains_aggregate",
    "eval_expr",
    "eval_row_expr",
    "execute_select",
    "is_sql_true",
    "iter_expr_children",
    "resolve_identifier",
    "sql_and",
    "sql_not",
    "sql_or",
    "to_logic",
    "validate_expr_columns",
    "walk_expr",
]
