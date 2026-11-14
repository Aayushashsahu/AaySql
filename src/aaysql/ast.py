from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type_name: str
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False
    default: Any = None
    has_default: bool = False


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class Identifier:
    name: str
    qualifier: str | None = None

    @property
    def label(self) -> str:
        return f"{self.qualifier}.{self.name}" if self.qualifier else self.name


@dataclass(frozen=True)
class AggregateCall:
    func: str
    arg: Any

    @property
    def label(self) -> str:
        if isinstance(self.arg, Identifier):
            return f"{self.func}({self.arg.label})"
        if isinstance(self.arg, Literal):
            val = "NULL" if self.arg.value is None else repr(self.arg.value)
            return f"{self.func}({val})"
        return f"{self.func}(...)"


@dataclass(frozen=True)
class UnaryExpr:
    op: str
    expr: Any


@dataclass(frozen=True)
class BinaryExpr:
    left: Any
    op: str
    right: Any


@dataclass(frozen=True)
class IsNull:
    """``expr IS NULL`` / ``expr IS NOT NULL``: never evaluates to UNKNOWN."""

    expr: Any
    negated: bool = False


@dataclass(frozen=True)
class IsMissing:
    """``expr IS MISSING`` / ``expr IS NOT MISSING``: checks ABSENT sentinel."""

    expr: Any
    negated: bool = False


@dataclass(frozen=True)
class IsJsonNull:
    """``expr IS JSON NULL`` / ``expr IS NOT JSON NULL``: checks explicit JSON null."""

    expr: Any
    negated: bool = False


@dataclass(frozen=True)
class PathAccess:
    """Path navigation into a JSON document: expr.path or expr[index]."""

    expr: Any
    path: tuple[str | int, ...]

    @property
    def label(self) -> str:
        base = self.expr.label if hasattr(self.expr, "label") else str(self.expr)
        segs = []
        for seg in self.path:
            if isinstance(seg, int):
                segs.append(f"[{seg}]")
            else:
                segs.append(f".{seg}")
        return f"{base}{''.join(segs)}"


@dataclass(frozen=True)
class FunctionCall:
    """SQL function call, e.g. JSON_EXTRACT(doc, path), JSON_SET(doc, path, val)."""

    func: str
    args: tuple[Any, ...]

    @property
    def label(self) -> str:
        return f"{self.func}(...)"


@dataclass(frozen=True)
class Like:
    """``expr LIKE pattern`` / ``expr NOT LIKE pattern`` (``%`` and ``_``)."""

    expr: Any
    pattern: Any
    negated: bool = False


@dataclass(frozen=True)
class InList:
    """``expr IN (a, b, ...)`` / ``expr NOT IN (...)`` using SQL equality."""

    expr: Any
    items: tuple[Any, ...]
    negated: bool = False


@dataclass(frozen=True)
class Between:
    """``expr BETWEEN low AND high`` (inclusive at both ends)."""

    expr: Any
    low: Any
    high: Any
    negated: bool = False


@dataclass(frozen=True)
class CaseWhen:
    """One ``WHEN <condition> THEN <result>`` branch of a CASE expression."""

    condition: Any
    result: Any


@dataclass(frozen=True)
class Case:
    """A searched (``base is None``) or simple CASE expression."""

    base: Any
    whens: tuple[CaseWhen, ...]
    else_expr: Any = None


@dataclass(frozen=True)
class SelectItem:
    expr: Any
    alias: str | None = None


@dataclass(frozen=True)
class JoinClause:
    join_type: str
    table: str
    alias: str | None = None
    condition: Any = None


@dataclass(frozen=True)
class CreateTable:
    name: str
    columns: list[ColumnDef]


@dataclass(frozen=True)
class CreateCollection:
    name: str


@dataclass(frozen=True)
class DropTable:
    name: str


@dataclass(frozen=True)
class Insert:
    table: str
    columns: list[str] | None
    values: list[list[Any]]


@dataclass(frozen=True)
class Select:
    columns: list[SelectItem]
    table: str
    where: Any | None = None
    order_by: list[tuple[Any, bool]] | None = None
    limit: int | None = None
    table_alias: str | None = None
    joins: list[JoinClause] = field(default_factory=list)
    group_by: list[Identifier] | None = None
    having: Any | None = None
    distinct: bool = False
    offset: int | None = None


@dataclass(frozen=True)
class Update:
    table: str
    assignments: list[tuple[str, Any]]
    where: Any | None = None


@dataclass(frozen=True)
class Delete:
    table: str
    where: Any | None = None


@dataclass(frozen=True)
class CreateIndex:
    name: str
    table: str
    column: str


@dataclass(frozen=True)
class DropIndex:
    name: str


@dataclass(frozen=True)
class Explain:
    statement: Any
    analyze: bool = False


@dataclass(frozen=True)
class Begin:
    isolation: str | None = None


@dataclass(frozen=True)
class Commit:
    pass


@dataclass(frozen=True)
class Rollback:
    pass
