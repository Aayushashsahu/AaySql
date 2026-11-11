from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ast import (
    AggregateCall,
    BinaryExpr,
    ColumnDef,
    Identifier,
    Literal,
    Select,
    SelectItem,
    UnaryExpr,
)
from .errors import ExecutionError
from .storage import Database, Table

_NUMERIC_TYPES = {"INT", "INTEGER", "FLOAT", "DOUBLE"}


@dataclass(frozen=True)
class SourceBinding:
    """A table bound to an effective reference name (alias or table name)."""

    ref: str
    table: Table
    column_map: dict[str, ColumnDef]

    @classmethod
    def from_table(cls, ref: str, table: Table) -> SourceBinding:
        return cls(
            ref=ref,
            table=table,
            column_map={col.name: col for col in table.columns},
        )


# --- SQL three-valued logic (TRUE / FALSE / UNKNOWN) ---
#
# UNKNOWN is represented by ``None``. Comparisons against NULL yield UNKNOWN,
# and UNKNOWN propagates through AND / OR / NOT instead of collapsing to FALSE.
# Only TRUE admits a row in WHERE, JOIN ON, and HAVING.


def to_logic(value: Any) -> bool | None:
    """Normalize a runtime value to a SQL logical state (``None`` means UNKNOWN)."""
    if value is None:
        return None
    return True if bool(value) else False


def sql_not(value: Any) -> bool | None:
    state = to_logic(value)
    return None if state is None else not state


def sql_and(left: Any, right: Any) -> bool | None:
    left_state = to_logic(left)
    right_state = to_logic(right)
    if left_state is False or right_state is False:
        return False
    if left_state is None or right_state is None:
        return None
    return True


def sql_or(left: Any, right: Any) -> bool | None:
    left_state = to_logic(left)
    right_state = to_logic(right)
    if left_state is True or right_state is True:
        return True
    if left_state is None or right_state is None:
        return None
    return False


def is_sql_true(value: Any) -> bool:
    """Row filters (WHERE, JOIN ON, HAVING) admit a row only when the result is TRUE."""
    return value is not None and bool(value)


def compare_values(op: str, left: Any, right: Any) -> bool | None:
    """Compare two runtime values using SQL comparison semantics.

    Returns ``None`` (UNKNOWN) when either operand is NULL, because a NULL
    comparison is neither TRUE nor FALSE in SQL.
    """
    normalized = op.upper()
    if normalized not in {"=", "!=", "<>", "<", "<=", ">", ">="}:
        raise ExecutionError(f"Unsupported operator '{op}'")
    if left is None or right is None:
        return None
    if normalized == "=":
        return left == right
    if normalized in {"!=", "<>"}:
        return left != right
    try:
        if normalized == "<":
            return left < right
        if normalized == "<=":
            return left <= right
        if normalized == ">":
            return left > right
        if normalized == ">=":
            return left >= right
    except TypeError as exc:
        raise ExecutionError(
            f"Cannot compare {type(left).__name__} and {type(right).__name__} with operator '{op}'"
        ) from exc
    raise ExecutionError(f"Unsupported operator '{op}'")


def resolve_identifier(ident: Identifier, sources: list[SourceBinding]) -> tuple[str, ColumnDef]:
    """Resolve a qualified or unqualified Identifier against active table sources."""
    if ident.qualifier is not None:
        source = next((s for s in sources if s.ref == ident.qualifier), None)
        if source is None:
            raise ExecutionError(f"Unknown table or alias '{ident.qualifier}'")
        col_def = source.column_map.get(ident.name)
        if col_def is None:
            raise ExecutionError(f"Unknown column '{ident.label}'")
        return source.ref, col_def

    if ident.name == "*":
        raise ExecutionError("Unexpected '*' in expression")

    matches = [(s.ref, s.column_map[ident.name]) for s in sources if ident.name in s.column_map]
    if not matches:
        raise ExecutionError(f"Unknown column '{ident.name}'")
    if len(matches) > 1:
        raise ExecutionError(f"Ambiguous column reference '{ident.name}'")
    return matches[0]


def contains_aggregate(expr: Any) -> bool:
    if isinstance(expr, AggregateCall):
        return True
    if isinstance(expr, UnaryExpr):
        return contains_aggregate(expr.expr)
    if isinstance(expr, BinaryExpr):
        return contains_aggregate(expr.left) or contains_aggregate(expr.right)
    return False


def _validate_no_aggregates(expr: Any, context_name: str) -> None:
    if isinstance(expr, AggregateCall):
        raise ExecutionError(f"Aggregate functions are not allowed in {context_name}")
    if isinstance(expr, UnaryExpr):
        _validate_no_aggregates(expr.expr, context_name)
    elif isinstance(expr, BinaryExpr):
        _validate_no_aggregates(expr.left, context_name)
        _validate_no_aggregates(expr.right, context_name)


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
    if expr is None or isinstance(expr, (Literal, str, int, float, bool)):
        return
    if isinstance(expr, Identifier):
        resolve_identifier(expr, sources)
        return
    if isinstance(expr, AggregateCall):
        if not allow_aggregates:
            raise ExecutionError(f"Aggregate functions are not allowed in {context_name}")
        _validate_aggregate_arg(expr, sources)
        return
    if isinstance(expr, UnaryExpr):
        validate_expr_columns(expr.expr, sources, allow_aggregates=allow_aggregates, context_name=context_name)
        return
    if isinstance(expr, BinaryExpr):
        validate_expr_columns(expr.left, sources, allow_aggregates=allow_aggregates, context_name=context_name)
        validate_expr_columns(expr.right, sources, allow_aggregates=allow_aggregates, context_name=context_name)
        return
    raise ExecutionError(f"Cannot evaluate expression {expr}")


def _validate_grouped_expr(
    expr: Any,
    sources: list[SourceBinding],
    grouped_keys: set[tuple[str, str]],
) -> None:
    """Ensure every non-aggregated column reference in expr belongs to GROUP BY."""
    if expr is None or isinstance(expr, (Literal, str, int, float, bool)):
        return
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
    if isinstance(expr, UnaryExpr):
        _validate_grouped_expr(expr.expr, sources, grouped_keys)
        return
    if isinstance(expr, BinaryExpr):
        _validate_grouped_expr(expr.left, sources, grouped_keys)
        _validate_grouped_expr(expr.right, sources, grouped_keys)
        return
    raise ExecutionError(f"Cannot evaluate expression {expr}")


def eval_expr(
    expr: Any,
    row_ctx: dict[str, dict[str, Any]] | None,
    sources: list[SourceBinding],
    aggregate_eval: Any | None = None,
) -> Any:
    """Evaluate an expression against a row context, optionally resolving aggregates."""
    if isinstance(expr, Literal):
        return expr.value
    if expr is None or isinstance(expr, (str, int, float, bool)):
        return expr
    if isinstance(expr, AggregateCall):
        if aggregate_eval is None:
            raise ExecutionError(f"Aggregate function '{expr.func}' is not allowed in this context")
        return aggregate_eval(expr)
    if isinstance(expr, Identifier):
        if row_ctx is None:
            return None
        ref, col_def = resolve_identifier(expr, sources)
        return row_ctx[ref][col_def.name]
    if isinstance(expr, UnaryExpr):
        if expr.op.upper() != "NOT":
            raise ExecutionError(f"Unsupported unary operator '{expr.op}'")
        return sql_not(eval_expr(expr.expr, row_ctx, sources, aggregate_eval))
    if isinstance(expr, BinaryExpr):
        op = expr.op.upper()
        left = eval_expr(expr.left, row_ctx, sources, aggregate_eval)
        right = eval_expr(expr.right, row_ctx, sources, aggregate_eval)
        if op == "AND":
            return sql_and(left, right)
        if op == "OR":
            return sql_or(left, right)
        return compare_values(op, left, right)
    raise ExecutionError(f"Cannot evaluate expression {expr}")


def eval_row_expr(expr: Any, row_ctx: dict[str, dict[str, Any]], sources: list[SourceBinding]) -> Any:
    """Evaluate a non-aggregate expression against a single joined/table row context."""
    return eval_expr(expr, row_ctx, sources)


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
