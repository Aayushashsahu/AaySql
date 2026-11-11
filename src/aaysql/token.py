from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    PREFIX = auto()
    IDENT = auto()
    NUMBER = auto()
    STRING = auto()
    STAR = auto()
    COMMA = auto()
    LPAREN = auto()
    RPAREN = auto()
    SEMICOLON = auto()
    DOT = auto()
    EQ = auto()
    NE = auto()
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()
    PLUS = auto()
    MINUS = auto()
    SLASH = auto()
    PERCENT = auto()
    EOF = auto()


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str | int | float | None
    position: int
