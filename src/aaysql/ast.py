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
