"""Canonical SQL expression semantics.

Every expression the engine can parse is represented by one of the AST nodes in
:mod:`aaysql.ast`, and every consumer -- validation, grouped-expression checks,
aggregate detection, planner predicate placement, EXPLAIN rendering, and the two
execution paths -- walks expressions through :func:`iter_expr_children` /
:func:`walk_expr`. Adding a node type in one place (that traversal) therefore
cannot silently bypass identifier validation, aggregate restrictions, or
predicate pushdown: an unhandled node is reported as unevaluable instead of
being skipped.

The evaluator in this module is the single implementation of row-level and
group-level semantics. The reference path (``Executor`` for UPDATE/DELETE) and
the plan-driven path (``execution.execute_select``) both call
:func:`eval_expr`, so arithmetic, LIKE, IN, BETWEEN, CASE, and three-valued
logic behave identically everywhere.

SQL three-valued logic: TRUE and FALSE are Python ``True`` / ``False``, and
UNKNOWN is ``None``. A row is admitted by WHERE / JOIN ON / HAVING only when the
predicate is TRUE.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .ast import (
    AggregateCall,
    Between,
    BinaryExpr,
    Case,
    ColumnDef,
    Identifier,
    InList,
    IsNull,
    Like,
    Literal,
    UnaryExpr,
)
from .errors import ExecutionError
from .storage import Table

NUMERIC_TYPES = frozenset({"INT", "INTEGER", "FLOAT", "DOUBLE"})

ARITHMETIC_OPS = frozenset({"+", "-", "*", "/", "%"})
COMPARISON_OPS = frozenset({"=", "!=", "<>", "<", "<=", ">", ">="})
LOGICAL_OPS = frozenset({"AND", "OR"})

#: Bounded cache for translated LIKE patterns; a plan never recompiles a pattern
#: per row, and memory stays constant regardless of how many patterns are used.
LIKE_CACHE_SIZE = 256

#: Every node type the traversal and evaluator understand.
EXPRESSION_TYPES = (
    Literal,
    Identifier,
    AggregateCall,
    UnaryExpr,
    BinaryExpr,
    IsNull,
    Like,
    InList,
    Between,
    Case,
)
_LEAF_TYPES = (Literal, Identifier)
_SCALARS = (str, int, float, bool)


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


# --- SQL three-valued logic (TRUE / FALSE / UNKNOWN) ----------------------


def to_logic(value: Any) -> bool | None:
    """Normalize a runtime value to a SQL logical state (``None`` means UNKNOWN)."""
    if value is None:
        return None
    return bool(value)


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


_TYPE_LABELS = {bool: "BOOL", int: "INT", float: "FLOAT", str: "TEXT"}


def _type_label(value: Any) -> str:
    return _TYPE_LABELS.get(type(value), type(value).__name__.upper())


def _reject_boolean_comparison(op: str, left: Any, right: Any) -> None:
    """A BOOL value never compares equal to a number in SQL.

    Python's ``True == 1`` is true, which would silently make ``WHERE flag = 1``
    match every true row. SQL treats BOOL as its own type, so a mixed BOOL/non-BOOL
    comparison is rejected rather than coerced.
    """
    if isinstance(left, bool) == isinstance(right, bool):
        return
    other = right if isinstance(left, bool) else left
    if other is None:
        return
    raise ExecutionError(
        f"Cannot compare BOOL with {_type_label(other)} using operator '{op}'; "
        "use TRUE/FALSE for boolean comparisons"
    )


def compare_values(op: str, left: Any, right: Any) -> bool | None:
    """Compare two runtime values using SQL comparison semantics.

    Returns ``None`` (UNKNOWN) when either operand is NULL, because a NULL
    comparison is neither TRUE nor FALSE in SQL. BOOL operands only compare with
    other BOOL operands; numbers compare numerically across INT/FLOAT; a TEXT and
    a numeric operand are unrelated and compare unequal (never an unevaluated
    exception).
    """
    normalized = op.upper()
    if normalized not in COMPARISON_OPS:
        raise ExecutionError(f"Unsupported operator '{op}'")
    if left is None or right is None:
        return None
    _reject_boolean_comparison(normalized, left, right)
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


# --- shared AST traversal -------------------------------------------------


def iter_expr_children(expr: Any) -> Iterator[Any]:
    """Yield the direct sub-expressions of ``expr`` in evaluation order.

    This is the only place that knows the shape of every expression node.
    """
    if isinstance(expr, BinaryExpr):
        yield expr.left
        yield expr.right
    elif isinstance(expr, UnaryExpr):
        yield expr.expr
    elif isinstance(expr, AggregateCall):
        yield expr.arg
    elif isinstance(expr, IsNull):
        yield expr.expr
    elif isinstance(expr, Like):
        yield expr.expr
        yield expr.pattern
    elif isinstance(expr, InList):
        yield expr.expr
        yield from expr.items
    elif isinstance(expr, Between):
        yield expr.expr
        yield expr.low
        yield expr.high
    elif isinstance(expr, Case):
        if expr.base is not None:
            yield expr.base
        for when in expr.whens:
            yield when.condition
            yield when.result
        if expr.else_expr is not None:
            yield expr.else_expr


def walk_expr(expr: Any) -> Iterator[Any]:
    """Pre-order traversal of an expression tree (leaves included)."""
    if expr is None or isinstance(expr, _SCALARS):
        return
    yield expr
    for child in iter_expr_children(expr):
        yield from walk_expr(child)


def ensure_known_expr(expr: Any) -> None:
    """Reject any node type the engine does not understand instead of ignoring it."""
    for node in walk_expr(expr):
        if not isinstance(node, EXPRESSION_TYPES):
            raise ExecutionError(f"Cannot evaluate expression {node}")


def contains_aggregate(expr: Any) -> bool:
    return any(isinstance(node, AggregateCall) for node in walk_expr(expr))


def collect_identifiers(expr: Any) -> list[Identifier]:
    """Every column reference reachable from ``expr`` (aggregate args included)."""
    return [node for node in walk_expr(expr) if isinstance(node, Identifier)]


# --- arithmetic -----------------------------------------------------------


def _require_numeric(value: Any, op: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionError(
            f"Operator '{op}' requires numeric operands, got {type(value).__name__}"
        )


def _require_finite(result: Any, op: str) -> Any:
    if isinstance(result, float) and not math.isfinite(result):
        kind = "NaN" if math.isnan(result) else "infinity"
        raise ExecutionError(
            f"Arithmetic result of '{op}' is not a finite number; refusing to produce {kind}"
        )
    return result


def eval_arithmetic(op: str, left: Any, right: Any) -> Any:
    """Evaluate one arithmetic operation with NULL propagation.

    ``INT`` operands stay ``int`` for ``+ - * %``; ``/`` (and any operation with a
    ``FLOAT`` operand) produces a float. Division and modulo by zero, non-numeric
    or BOOL operands, and non-finite results raise :class:`ExecutionError`.
    """
    if left is None or right is None:
        return None
    _require_numeric(left, op)
    _require_numeric(right, op)
    if op == "+":
        result = left + right
    elif op == "-":
        result = left - right
    elif op == "*":
        result = left * right
    elif op == "/":
        if right == 0:
            raise ExecutionError("Division by zero")
        result = left / right
    elif op == "%":
        if right == 0:
            raise ExecutionError("Modulo by zero")
        result = left % right
    else:
        raise ExecutionError(f"Unsupported arithmetic operator '{op}'")
    return _require_finite(result, op)


def eval_unary_arithmetic(op: str, value: Any) -> Any:
    """Evaluate unary ``+`` / ``-`` with NULL propagation."""
    if value is None:
        return None
    _require_numeric(value, op)
    return _require_finite(-value if op == "-" else +value, op)


# --- LIKE -----------------------------------------------------------------


@lru_cache(maxsize=LIKE_CACHE_SIZE)
def _compiled_like(pattern: str) -> re.Pattern[str]:
    """Translate a SQL LIKE pattern into an anchored, fully escaped regex.

    Only ``%`` (zero or more characters) and ``_`` (exactly one character) are
    special. Every other character is escaped with ``re.escape``, so a user
    pattern is never executed as a regular expression, and the result is anchored
    on both ends to implement whole-value matching.
    """
    parts = [r"\A"]
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    parts.append(r"\Z")
    return re.compile("".join(parts), re.DOTALL)


def eval_like(value: Any, pattern: Any) -> bool | None:
    """Case-sensitive LIKE. NULL on either side yields UNKNOWN."""
    if value is None or pattern is None:
        return None
    if not isinstance(value, str):
        raise ExecutionError(
            f"LIKE requires a TEXT operand, got {type(value).__name__}"
        )
    if not isinstance(pattern, str):
        raise ExecutionError(
            f"LIKE requires a TEXT pattern, got {type(pattern).__name__}"
        )
    return _compiled_like(pattern).match(value) is not None


# --- IN / BETWEEN ---------------------------------------------------------


def eval_in(value: Any, candidates: list[Any], negated: bool = False) -> bool | None:
    """SQL ``IN`` membership with three-valued logic (``NOT IN`` is its negation)."""
    if value is None:
        return None
    saw_null = False
    for candidate in candidates:
        comparison = compare_values("=", value, candidate)
        if comparison is None:
            saw_null = True
        elif comparison:
            return not negated
    if saw_null:
        return None
    return negated


def eval_between(value: Any, low: Any, high: Any, negated: bool = False) -> bool | None:
    """Inclusive ``BETWEEN``: ``value >= low AND value <= high``."""
    result = sql_and(compare_values(">=", value, low), compare_values("<=", value, high))
    return sql_not(result) if negated else result


# --- evaluation -----------------------------------------------------------


def eval_expr(
    expr: Any,
    row_ctx: dict[str, dict[str, Any]] | None,
    sources: list[SourceBinding],
    aggregate_eval: Any | None = None,
) -> Any:
    """Evaluate an expression against a row context, optionally resolving aggregates."""
    if isinstance(expr, Literal):
        return expr.value
    if expr is None or isinstance(expr, _SCALARS):
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
        op = expr.op.upper()
        if op == "NOT":
            return sql_not(eval_expr(expr.expr, row_ctx, sources, aggregate_eval))
        if op in {"+", "-"}:
            return eval_unary_arithmetic(op, eval_expr(expr.expr, row_ctx, sources, aggregate_eval))
        raise ExecutionError(f"Unsupported unary operator '{expr.op}'")
    if isinstance(expr, BinaryExpr):
        op = expr.op.upper()
        left = eval_expr(expr.left, row_ctx, sources, aggregate_eval)
        right = eval_expr(expr.right, row_ctx, sources, aggregate_eval)
        if op == "AND":
            return sql_and(left, right)
        if op == "OR":
            return sql_or(left, right)
        if op in ARITHMETIC_OPS:
            return eval_arithmetic(op, left, right)
        return compare_values(op, left, right)
    if isinstance(expr, IsNull):
        value = eval_expr(expr.expr, row_ctx, sources, aggregate_eval)
        return (value is not None) if expr.negated else (value is None)
    if isinstance(expr, Like):
        value = eval_expr(expr.expr, row_ctx, sources, aggregate_eval)
        pattern = eval_expr(expr.pattern, row_ctx, sources, aggregate_eval)
        matched = eval_like(value, pattern)
        return sql_not(matched) if expr.negated else matched
    if isinstance(expr, InList):
        value = eval_expr(expr.expr, row_ctx, sources, aggregate_eval)
        candidates = [eval_expr(item, row_ctx, sources, aggregate_eval) for item in expr.items]
        return eval_in(value, candidates, expr.negated)
    if isinstance(expr, Between):
        value = eval_expr(expr.expr, row_ctx, sources, aggregate_eval)
        low = eval_expr(expr.low, row_ctx, sources, aggregate_eval)
        high = eval_expr(expr.high, row_ctx, sources, aggregate_eval)
        return eval_between(value, low, high, expr.negated)
    if isinstance(expr, Case):
        return _eval_case(expr, row_ctx, sources, aggregate_eval)
    raise ExecutionError(f"Cannot evaluate expression {expr}")


def _eval_case(
    expr: Case,
    row_ctx: dict[str, dict[str, Any]] | None,
    sources: list[SourceBinding],
    aggregate_eval: Any | None,
) -> Any:
    """Evaluate CASE: first TRUE branch wins; UNKNOWN never matches."""
    if expr.base is None:
        for when in expr.whens:
            condition = eval_expr(when.condition, row_ctx, sources, aggregate_eval)
            if is_sql_true(condition):
                return eval_expr(when.result, row_ctx, sources, aggregate_eval)
        if expr.else_expr is None:
            return None
        return eval_expr(expr.else_expr, row_ctx, sources, aggregate_eval)

    base_value = eval_expr(expr.base, row_ctx, sources, aggregate_eval)
    if base_value is not None:
        for when in expr.whens:
            candidate = eval_expr(when.condition, row_ctx, sources, aggregate_eval)
            # SQL comparison, not Python equality: a NULL operand never matches
            # even when the other side is also NULL.
            if compare_values("=", base_value, candidate) is True:
                return eval_expr(when.result, row_ctx, sources, aggregate_eval)
    if expr.else_expr is None:
        return None
    return eval_expr(expr.else_expr, row_ctx, sources, aggregate_eval)


def eval_row_expr(expr: Any, row_ctx: dict[str, dict[str, Any]], sources: list[SourceBinding]) -> Any:
    """Evaluate a non-aggregate expression against a single joined/table row context."""
    return eval_expr(expr, row_ctx, sources)
