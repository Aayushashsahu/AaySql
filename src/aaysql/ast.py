from dataclasses import dataclass
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
    order_by: list[tuple[str, bool]] | None = None
    limit: int | None = None


@dataclass(frozen=True)
class Update:
    table: str
    assignments: list[tuple[str, Any]]
    where: Any | None = None


@dataclass(frozen=True)
class Delete:
    table: str
    where: Any | None = None
