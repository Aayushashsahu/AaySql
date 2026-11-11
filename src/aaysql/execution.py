"""Plan-driven SELECT execution.

The executor walks the physical plan produced by :mod:`aaysql.planner`. Scans and
joins run exactly as planned (access path, pushed predicates, join algorithm);
the record-level stages -- ``GROUP BY``/``HAVING``, projection, ``ORDER BY``,
``LIMIT`` -- follow the plan's wrapper nodes in SQL semantic order.

Row order is part of the observable contract. Every join algorithm emits rows in
left-input order and, for each left row, in right-input order, which is exactly
what the reference nested-loop implementation produces. The hash join is an
algorithmic substitution only: same rows, same order.
"""

from __future__ import annotations

import time
from typing import Any

from .ast import AggregateCall, Identifier, Select
from .errors import ExecutionError
from .planner import PlanNode, QueryPlan, plan_select
from .query import (
    SourceBinding,
    _default_expr_label,
    _eval_group_expr,
    _is_wildcard_select,
    _normalize_order_expr,
    _validate_grouped_expr,
    _validate_no_aggregates,
    _validate_output_labels,
    contains_aggregate,
    eval_row_expr,
    is_sql_true,
    resolve_identifier,
    validate_expr_columns,
)
from .storage import Database

_WRAPPER_OPS = ("FILTER", "AGGREGATE", "PROJECTION", "DISTINCT", "SORT", "OFFSET", "LIMIT")
_SCAN_OPS = ("TABLE SCAN", "INDEX SCAN")


def _distinct_key(values: list[Any]) -> tuple[Any, ...]:
    """Canonical duplicate-elimination key: NULLs are equal, types stay apart.

    Tagging each value with its Python type keeps ``True`` distinct from ``1`` and
    ``1`` distinct from ``1.0``, which SQL column semantics also keep apart, while
    ``("NULL",)`` makes every NULL duplicate the same.
    """
    return tuple(
        ("NULL",) if value is None else (type(value).__name__, value)
        for value in values
    )


def _distinct_order_target(
    stmt: Select,
    order_expr: Any,
    sources: list[SourceBinding],
    cols: list[str],
    alias_counts: dict[str, int],
    is_wildcard: bool,
) -> str:
    """Resolve which projected column a DISTINCT ORDER BY key sorts by.

    Duplicate elimination happens on the projected row, so ordering must refer to
    that same row. An ORDER BY key is therefore required to be a unique SELECT
    alias, a selected expression, or (for ``SELECT DISTINCT *``) a selected column.
    """
    if isinstance(order_expr, Identifier):
        if order_expr.qualifier is None and alias_counts.get(order_expr.name) == 1:
            return order_expr.name
        if is_wildcard:
            ref, col_def = resolve_identifier(order_expr, sources)
            return f"{ref}.{col_def.name}" if stmt.joins else col_def.name
    for index, item in enumerate(stmt.columns):
        if item.expr == order_expr:
            return cols[index]
    raise ExecutionError(
        "For SELECT DISTINCT, ORDER BY expressions must appear in the select list"
    )


def _source_for(ref: str, sources: list[SourceBinding]) -> SourceBinding:
    for source in sources:
        if source.ref == ref:
            return source
    raise ExecutionError(f"Unknown table or alias '{ref}'")


def _all_true(predicates: list[Any], ctx: dict[str, dict[str, Any]], sources: list[SourceBinding]) -> bool:
    """Conjuncts are combined with SQL AND: all of them must be TRUE."""
    for predicate in predicates:
        if not is_sql_true(eval_row_expr(predicate, ctx, sources)):
            return False
    return True


def _scan_rows(
    db: Database,
    node: PlanNode,
    sources: list[SourceBinding],
) -> tuple[list[dict[str, dict[str, Any]]], int, int]:
    table = db.table(node.table)
    if node.index_name is None:
        rows = list(table.rows)
    else:
        index = db.indexes.get(node.index_name)
        if index is None:
            raise ExecutionError(
                f"Index '{node.index_name}' does not exist; cannot execute the planned index scan"
            )
        rows = index.range(
            start=node.start,
            end=node.end,
            start_inclusive=node.start_inclusive,
            end_inclusive=node.end_inclusive,
        )
    rows_read = len(rows)
    if not node.predicates:
        return [{node.ref: row} for row in rows], rows_read, 0
    kept: list[dict[str, dict[str, Any]]] = []
    for row in rows:
        ctx = {node.ref: row}
        if _all_true(node.predicates, ctx, sources):
            kept.append(ctx)
    return kept, rows_read, rows_read - len(kept)


def _hash_key(
    ctx: dict[str, dict[str, Any]],
    key_pairs: list[tuple[str, str, str, str]],
    side: str,
) -> tuple[Any, ...] | None:
    """Build the equality key for one side; a NULL key column never matches."""
    values: list[Any] = []
    for left_ref, left_column, right_ref, right_column in key_pairs:
        if side == "LEFT":
            value = ctx[left_ref][left_column]
        else:
            value = ctx[right_ref][right_column]
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _null_row(ref: str, sources: list[SourceBinding]) -> dict[str, Any]:
    return {column: None for column in _source_for(ref, sources).table.column_names}


def _run_scan(
    db: Database,
    node: PlanNode,
    sources: list[SourceBinding],
    state: dict[str, Any],
) -> list[dict[str, dict[str, Any]]]:
    started = time.perf_counter()
    rows, rows_read, rejected = _scan_rows(db, node, sources)
    elapsed = (time.perf_counter() - started) * 1000.0
    state["rows_scanned"] += rows_read
    state["rows_filtered"] += rejected
    if node.index_name is not None:
        state["indexes"].append(node.index_name)
    node.actual.update(rows_in=rows_read, rows_out=len(rows), rejected=rejected, time_ms=elapsed)
    target = f"{node.index_name} on {node.table}" if node.index_name else node.table
    state["lines"].append(
        f"{node.op} {target}: rows_read={rows_read} rows_out={len(rows)} "
        f"rejected={rejected} est_rows=~{node.estimated_rows:.0f} time={elapsed:.3f}ms"
    )
    return rows


def _run_join(
    db: Database,
    node: PlanNode,
    sources: list[SourceBinding],
    state: dict[str, Any],
) -> list[dict[str, dict[str, Any]]]:
    left = _run_node(db, node.children[0], sources, state)
    right = _run_node(db, node.children[1], sources, state)
    right_ref = node.children[1].ref
    null_right = _null_row(right_ref, sources)
    started = time.perf_counter()
    rows: list[dict[str, dict[str, Any]]] = []
    state["join_algorithms"].append(node.algorithm)
    counters: dict[str, Any] = {}

    if node.algorithm == "HASH":
        buckets: dict[tuple[Any, ...], list[dict[str, dict[str, Any]]]] = {}
        build_rows = 0
        for right_ctx in right:
            key = _hash_key(right_ctx, node.key_pairs, "RIGHT")
            if key is None:
                continue
            buckets.setdefault(key, []).append(right_ctx)
            build_rows += 1
        probe_rows = 0
        bucket_matches = 0
        residual_rejected = 0
        for left_ctx in left:
            probe_rows += 1
            key = _hash_key(left_ctx, node.key_pairs, "LEFT")
            bucket = () if key is None else buckets.get(key, ())
            matched = False
            for right_ctx in bucket:
                bucket_matches += 1
                candidate = {**left_ctx, **right_ctx}
                if is_sql_true(eval_row_expr(node.on, candidate, sources)):
                    rows.append(candidate)
                    matched = True
                else:
                    residual_rejected += 1
            if node.join_type == "LEFT" and not matched:
                rows.append({**left_ctx, right_ref: dict(null_right)})
        counters = {
            "build_rows": build_rows,
            "probe_rows": probe_rows,
            "bucket_matches": bucket_matches,
            "residual_rejected": residual_rejected,
        }
    else:
        comparisons = 0
        for left_ctx in left:
            matched = False
            for right_ctx in right:
                candidate = {**left_ctx, **right_ctx}
                comparisons += 1
                if is_sql_true(eval_row_expr(node.on, candidate, sources)):
                    rows.append(candidate)
                    matched = True
            if node.join_type == "LEFT" and not matched:
                rows.append({**left_ctx, right_ref: dict(null_right)})
        counters = {"comparisons": comparisons}

    elapsed = (time.perf_counter() - started) * 1000.0
    node.actual.update(
        left_rows=len(left),
        right_rows=len(right),
        rows_out=len(rows),
        time_ms=elapsed,
        **counters,
    )
    details = " ".join(f"{key}={value}" for key, value in counters.items())
    state["lines"].append(
        f"{node.algorithm} JOIN ({node.join_type}): rows_in={len(left)}+{len(right)} "
        f"rows_out={len(rows)} {details} est_rows=~{node.estimated_rows:.0f} time={elapsed:.3f}ms"
    )
    return rows


def _run_node(
    db: Database,
    node: PlanNode,
    sources: list[SourceBinding],
    state: dict[str, Any],
) -> list[dict[str, dict[str, Any]]]:
    if node.op in _SCAN_OPS:
        return _run_scan(db, node, sources, state)
    if node.op == "JOIN":
        return _run_join(db, node, sources, state)
    raise ExecutionError(f"Unsupported plan operator '{node.op}'")


def _validation_and_bindings(db: Database, stmt: Select) -> tuple[list[SourceBinding], list[str], bool]:
    """Bind sources and validate the statement, preserving the original errors."""
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
    if stmt.distinct and stmt.order_by:
        for raw_expr, _ in stmt.order_by:
            _distinct_order_target(
                stmt,
                _normalize_order_expr(raw_expr),
                sources,
                cols,
                explicit_alias_counts,
                is_wildcard,
            )
    return sources, cols, is_grouped_query


def execute_select(
    db: Database,
    stmt: Select,
    plan: QueryPlan | None = None,
    collect_metrics: bool = False,
    *,
    force_join_algorithm: str | None = None,
    force_access_path: str | None = None,
) -> dict[str, Any]:
    """Execute a SELECT by running the physical plan selected for it."""
    sources, cols, is_grouped_query = _validation_and_bindings(db, stmt)
    is_wildcard = _is_wildcard_select(stmt.columns)
    has_group_by = bool(stmt.group_by)

    if plan is None or plan.root is None:
        plan = plan_select(
            db,
            stmt,
            force_join_algorithm=force_join_algorithm,
            force_access_path=force_access_path,
        )
    root = plan.root

    state: dict[str, Any] = {
        "rows_scanned": 0,
        "rows_filtered": 0,
        "lines": [],
        "indexes": [],
        "join_algorithms": [],
    }

    wrappers: dict[str, PlanNode] = {}
    node = root
    while node.op in _WRAPPER_OPS:
        wrappers[node.op] = node
        node = node.children[0]
    working_rows = _run_node(db, node, sources, state)

    filter_node = wrappers.get("FILTER")
    if filter_node is not None:
        before = len(working_rows)
        working_rows = [
            ctx for ctx in working_rows if _all_true(filter_node.predicates, ctx, sources)
        ]
        rejected = before - len(working_rows)
        state["rows_filtered"] += rejected
        filter_node.actual.update(rows_in=before, rows_out=len(working_rows), rejected=rejected)
        state["lines"].append(
            f"FILTER (post-join): rows_in={before} rows_out={len(working_rows)} "
            f"rejected={rejected} est_rows=~{filter_node.estimated_rows:.0f}"
        )

    records: list[tuple[dict[str, Any], dict[str, dict[str, Any]] | None, list[dict[str, dict[str, Any]]] | None]] = []
    aggregate_node = wrappers.get("AGGREGATE")
    if is_grouped_query:
        if has_group_by:
            grouped: dict[tuple[tuple[type, Any], ...], tuple[dict[str, dict[str, Any]], list[dict[str, dict[str, Any]]]]] = {}
            resolved_group_refs = [resolve_identifier(g, sources) for g in stmt.group_by or []]
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

        if aggregate_node is not None:
            aggregate_node.actual.update(rows_in=len(working_rows), rows_out=len(records))
            state["lines"].append(
                f"AGGREGATE ({aggregate_node.detail}): rows_in={len(working_rows)} "
                f"rows_out={len(records)} est_rows=~{aggregate_node.estimated_rows:.0f}"
            )
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

    projection_node = wrappers.get("PROJECTION")
    if projection_node is not None:
        projection_node.actual.update(rows_in=len(records), rows_out=len(records))
        state["lines"].append(f"PROJECTION: rows_in={len(records)} rows_out={len(records)}")

    explicit_alias_counts: dict[str, int] = {}
    for item in stmt.columns:
        if item.alias:
            explicit_alias_counts[item.alias] = explicit_alias_counts.get(item.alias, 0) + 1

    # DISTINCT removes duplicate projected rows after projection and before
    # ORDER BY, keeping the first occurrence so unordered output stays stable.
    if stmt.distinct:
        rows_before_distinct = len(records)
        seen: set[tuple[Any, ...]] = set()
        kept: list[tuple[dict[str, Any], Any, Any]] = []
        for rec in records:
            key = _distinct_key([rec[0][name] for name in cols])
            if key in seen:
                continue
            seen.add(key)
            kept.append(rec)
        records = kept
        removed = rows_before_distinct - len(records)
        distinct_node = wrappers.get("DISTINCT")
        if distinct_node is not None:
            distinct_node.actual.update(
                rows_in=rows_before_distinct,
                rows_out=len(records),
                duplicates_removed=removed,
            )
        state["lines"].append(
            f"DISTINCT: rows_in={rows_before_distinct} rows_out={len(records)} "
            f"duplicates_removed={removed}"
        )

    if stmt.order_by:
        sort_node = wrappers.get("SORT")
        rows_before_sort = len(records)
        for raw_expr, desc in reversed(stmt.order_by):
            order_expr = _normalize_order_expr(raw_expr)
            use_alias = (
                isinstance(order_expr, Identifier)
                and order_expr.qualifier is None
                and explicit_alias_counts.get(order_expr.name, 0) == 1
            )
            projected_key = None
            if stmt.distinct:
                projected_key = _distinct_order_target(
                    stmt,
                    order_expr,
                    sources,
                    cols,
                    explicit_alias_counts,
                    is_wildcard,
                )
            keyed: list[tuple[Any, tuple[dict[str, Any], Any, Any]]] = []
            for rec in records:
                out_row, ctx_or_sample, group_rows = rec
                if projected_key is not None:
                    sort_val = out_row[projected_key]
                elif use_alias:
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
        if sort_node is not None:
            sort_node.actual.update(rows_in=rows_before_sort, rows_out=len(records))
            state["lines"].append(f"SORT: rows_in={rows_before_sort} rows_out={len(records)}")

    # OFFSET is applied after ordering and before LIMIT.
    if stmt.offset:
        rows_before_offset = len(records)
        records = records[stmt.offset:]
        discarded = rows_before_offset - len(records)
        offset_node = wrappers.get("OFFSET")
        if offset_node is not None:
            offset_node.actual.update(
                rows_in=rows_before_offset,
                rows_out=len(records),
                rows_discarded=discarded,
            )
        state["lines"].append(
            f"OFFSET: discarded={discarded} rows_in={rows_before_offset} rows_out={len(records)}"
        )

    if stmt.limit is not None:
        rows_before_limit = len(records)
        records = records[: stmt.limit]
        limit_node = wrappers.get("LIMIT")
        if limit_node is not None:
            limit_node.actual.update(rows_in=rows_before_limit, rows_out=len(records))
            state["lines"].append(f"LIMIT: rows_in={rows_before_limit} rows_out={len(records)}")

    data = [out_row for out_row, _, _ in records]
    result: dict[str, Any] = {
        "status": "ok",
        "columns": cols,
        "rows": data,
        "row_count": len(data),
    }
    if collect_metrics:
        algorithms = state["join_algorithms"]
        indexes = state["indexes"]
        result["metrics"] = {
            "rows_scanned": state["rows_scanned"],
            "rows_filtered": state["rows_filtered"],
            "rows_produced": len(data),
            "index": indexes[0] if indexes else None,
            "join_algorithm": algorithms[-1] if algorithms else None,
            "operator_lines": state["lines"],
        }
    return result
