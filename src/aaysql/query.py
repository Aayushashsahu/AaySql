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
from .planner import fetch_plan_rows
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
) -> dict[str, Any]:
    """Execute a SELECT through source binding, JOIN, WHERE, GROUP BY, HAVING, projection, ORDER BY, and LIMIT.

    When ``plan`` selects an index scan, candidate rows come from that index. The
    complete WHERE clause is always re-applied afterwards, so indexed and unindexed
    execution return identical rows.
    """
    base_ref = stmt.table_alias or stmt.table
    sources: list[SourceBinding] = [SourceBinding.from_table(base_ref, db.table(stmt.table))]
    seen_refs = {base_ref}

    for join in stmt.joins:
        join_ref = join.alias or join.table
        if join_ref in seen_refs:
            raise ExecutionError(f"Duplicate table name or alias '{join_ref}' in FROM/JOIN clause")
        seen_refs.add(join_ref)
        join_table = db.table(join.table)
        join_source = SourceBinding.from_table(join_ref, join_table)
        scoped_sources = [*sources, join_source]
        _validate_no_aggregates(join.condition, "JOIN ON clause")
        validate_expr_columns(join.condition, scoped_sources, allow_aggregates=False, context_name="JOIN ON clause")
        sources.append(join_source)

    if stmt.where is not None:
        _validate_no_aggregates(stmt.where, "WHERE clause")
        validate_expr_columns(stmt.where, sources, allow_aggregates=False, context_name="WHERE clause")

    is_wildcard = _is_wildcard_select(stmt.columns)
    has_group_by = bool(stmt.group_by)
    has_aggregates = (
        any(contains_aggregate(item.expr) for item in stmt.columns)
        or (stmt.having is not None and contains_aggregate(stmt.having))
        or (stmt.order_by is not None and any(contains_aggregate(expr) for expr, _ in stmt.order_by))
    )
    is_grouped_query = has_group_by or has_aggregates or stmt.having is not None

    explicit_alias_counts: dict[str, int] = {}
    for item in stmt.columns:
        if item.alias:
            explicit_alias_counts[item.alias] = explicit_alias_counts.get(item.alias, 0) + 1

    grouped_keys: set[tuple[str, str]] = set()
    group_idents = stmt.group_by or []
    if is_grouped_query:
        for g_ident in group_idents:
            ref, col_def = resolve_identifier(g_ident, sources)
            grouped_keys.add((ref, col_def.name))

        if is_wildcard:
            for src in sources:
                for col_name in src.table.column_names:
                    if (src.ref, col_name) not in grouped_keys:
                        raise ExecutionError(
                            f"Column '{src.ref}.{col_name}' must appear in GROUP BY clause or be used in an aggregate function"
                        )
        else:
            for item in stmt.columns:
                _validate_grouped_expr(item.expr, sources, grouped_keys)
        if stmt.having is not None:
            _validate_grouped_expr(stmt.having, sources, grouped_keys)
    else:
        if not is_wildcard:
            for item in stmt.columns:
                validate_expr_columns(item.expr, sources, allow_aggregates=False, context_name="SELECT list")

    if stmt.order_by:
        for raw_expr, _ in stmt.order_by:
            order_expr = _normalize_order_expr(raw_expr)
            if isinstance(order_expr, Identifier) and order_expr.qualifier is None:
                alias_count = explicit_alias_counts.get(order_expr.name, 0)
                if alias_count > 1:
                    raise ExecutionError(f"Ambiguous ORDER BY alias '{order_expr.name}'")
                if alias_count == 1:
                    continue
            if is_grouped_query:
                _validate_grouped_expr(order_expr, sources, grouped_keys)
            else:
                validate_expr_columns(order_expr, sources, allow_aggregates=False, context_name="ORDER BY clause")

    if is_wildcard:
        if not stmt.joins:
            cols = list(sources[0].table.column_names)
        else:
            cols = [f"{src.ref}.{col}" for src in sources for col in src.table.column_names]
    else:
        cols = [
            item.alias or _default_expr_label(item.expr, i)
            for i, item in enumerate(stmt.columns)
        ]
    _validate_output_labels(cols)

    if plan is not None and plan.index_name is not None and not stmt.joins:
        base_rows = fetch_plan_rows(db, plan)
    else:
        base_rows = list(sources[0].table.rows)
    rows_scanned = len(base_rows)

    active_sources = [sources[0]]
    working_rows: list[dict[str, dict[str, Any]]] = [
        {sources[0].ref: row} for row in base_rows
    ]

    for idx, join in enumerate(stmt.joins):
        right_source = sources[idx + 1]
        active_sources = [*active_sources, right_source]
        null_right = {col: None for col in right_source.table.column_names}
        next_rows: list[dict[str, dict[str, Any]]] = []
        for left_ctx in working_rows:
            matched = False
            for right_row in right_source.table.rows:
                candidate = {**left_ctx, right_source.ref: right_row}
                if is_sql_true(eval_row_expr(join.condition, candidate, active_sources)):
                    next_rows.append(candidate)
                    matched = True
            if join.join_type == "LEFT" and not matched:
                next_rows.append({**left_ctx, right_source.ref: dict(null_right)})
        working_rows = next_rows

    if stmt.where is not None:
        working_rows = [
            ctx for ctx in working_rows if is_sql_true(eval_row_expr(stmt.where, ctx, sources))
        ]

    records: list[tuple[dict[str, Any], dict[str, dict[str, Any]] | None, list[dict[str, dict[str, Any]]] | None]] = []

    if is_grouped_query:
        if has_group_by:
            grouped: dict[tuple[tuple[type, Any], ...], tuple[dict[str, dict[str, Any]], list[dict[str, dict[str, Any]]]]] = {}
            resolved_group_refs = [resolve_identifier(g, sources) for g in group_idents]
            for ctx in working_rows:
                key_parts = []
                for ref, col_def in resolved_group_refs:
                    val = ctx[ref][col_def.name]
                    key_parts.append((type(None), None) if val is None else (type(val), val))
                key = tuple(key_parts)
                if key not in grouped:
                    grouped[key] = (ctx, [ctx])
                else:
                    grouped[key][1].append(ctx)
            groups = list(grouped.values())
        else:
            groups = [(working_rows[0] if working_rows else None, working_rows)]

        if stmt.having is not None:
            groups = [
                (sample_ctx, group_rows)
                for sample_ctx, group_rows in groups
                if is_sql_true(_eval_group_expr(stmt.having, sample_ctx, group_rows, sources))
            ]

        for sample_ctx, group_rows in groups:
            if is_wildcard:
                if not stmt.joins:
                    out = sample_ctx[sources[0].ref].copy() if sample_ctx is not None else {}
                else:
                    out = {
                        f"{src.ref}.{col}": (sample_ctx[src.ref][col] if sample_ctx is not None else None)
                        for src in sources
                        for col in src.table.column_names
                    }
            else:
                out = {
                    cols[i]: _eval_group_expr(item.expr, sample_ctx, group_rows, sources)
                    for i, item in enumerate(stmt.columns)
                }
            records.append((out, sample_ctx, group_rows))
    else:
        for ctx in working_rows:
            if is_wildcard:
                if not stmt.joins:
                    out = ctx[sources[0].ref].copy()
                else:
                    out = {
                        f"{src.ref}.{col}": ctx[src.ref][col]
                        for src in sources
                        for col in src.table.column_names
                    }
            else:
                out = {
                    cols[i]: eval_row_expr(item.expr, ctx, sources)
                    for i, item in enumerate(stmt.columns)
                }
            records.append((out, ctx, None))

    if stmt.order_by:
        for raw_expr, desc in reversed(stmt.order_by):
            order_expr = _normalize_order_expr(raw_expr)
            use_alias = (
                isinstance(order_expr, Identifier)
                and order_expr.qualifier is None
                and explicit_alias_counts.get(order_expr.name, 0) == 1
            )
            keyed: list[tuple[Any, tuple[dict[str, Any], Any, Any]]] = []
            for rec in records:
                out_row, ctx_or_sample, group_rows = rec
                if use_alias:
                    sort_val = out_row[order_expr.name]
                elif is_grouped_query:
                    sort_val = _eval_group_expr(order_expr, ctx_or_sample, group_rows or [], sources)
                else:
                    sort_val = eval_row_expr(order_expr, ctx_or_sample, sources)
                keyed.append((sort_val, rec))
            try:
                keyed.sort(key=lambda pair: (pair[0] is None, "" if pair[0] is None else pair[0]), reverse=desc)
            except TypeError as exc:
                label = order_expr.label if isinstance(order_expr, (Identifier, AggregateCall)) else str(order_expr)
                raise ExecutionError(f"Cannot sort column '{label}' due to incompatible values") from exc
            records = [rec for _, rec in keyed]

    if stmt.limit is not None:
        records = records[: stmt.limit]

    data = [out_row for out_row, _, _ in records]
    result: dict[str, Any] = {
        "status": "ok",
        "columns": cols,
        "rows": data,
        "row_count": len(data),
    }
    if collect_metrics:
        result["metrics"] = {
            "rows_scanned": rows_scanned,
            "rows_filtered": max(rows_scanned - len(working_rows), 0),
            "rows_produced": len(data),
            "index": plan.index_name if plan is not None else None,
            "join_algorithm": "NESTED LOOP" if stmt.joins else None,
        }
    return result
