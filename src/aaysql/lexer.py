import re

from .errors import LexerError
from .token import Token, TokenType

_KEYWORDS = {"AAYUSH"}


class Lexer:
    # Numbers are unsigned here on purpose: ``1-2`` must tokenize as three
    # tokens (number, minus, number) so binary subtraction and unary negation are
    # unambiguous. A leading sign is part of an expression, not part of a
    # literal, and the parser folds ``-<number literal>`` back into a literal.
    _pattern = re.compile(
        r"""
        (?P<WS>\s+)|
        (?P<COMMENT>--[^\n]*)|
        (?P<STRING>'(?:''|[^'])*'|"(?:""|[^"])*")|
        (?P<NUMBER>\d+(?:\.\d+)?)|
        (?P<OP><>|!=|<=|>=|=|<|>)|
        (?P<STAR>\*)|
        (?P<PLUS>\+)|
        (?P<MINUS>-)|
        (?P<SLASH>/)|
        (?P<PERCENT>%)|
        (?P<COMMA>,)|
        (?P<LPAREN>\()|
        (?P<RPAREN>\))|
        (?P<LBRACKET>\[)|
        (?P<RBRACKET>\])|
        (?P<LBRACE>\{)|
        (?P<RBRACE>\})|
        (?P<SEMICOLON>;)|
        (?P<DOT>\.)|
        (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
        """,
        re.VERBOSE,
    )

    def tokenize(self, text: str) -> list[Token]:
        tokens: list[Token] = []
        pos = 0
        while pos < len(text):
            match = self._pattern.match(text, pos)
            if not match:
                raise LexerError(f"Unexpected character at position {pos}: {text[pos]!r}")
            kind = match.lastgroup
            raw = match.group()
            start = pos
            pos = match.end()
            if kind in {"WS", "COMMENT"}:
                continue
            if kind == "IDENT":
                upper = raw.upper()
                tokens.append(
                    Token(
                        TokenType.PREFIX if upper in _KEYWORDS else TokenType.IDENT,
                        upper if upper in _KEYWORDS else raw,
                        start,
                    )
                )
            elif kind == "STRING":
                quote = raw[0]
                body = raw[1:-1]
                escaped = body.replace(quote * 2, quote)
                tokens.append(Token(TokenType.STRING, escaped, start))
            elif kind == "NUMBER":
                value = float(raw) if "." in raw else int(raw)
                tokens.append(Token(TokenType.NUMBER, value, start))
            else:
                mapping = {
                    "OP": {
                        "=": TokenType.EQ,
                        "!=": TokenType.NE,
                        "<>": TokenType.NE,
                        "<": TokenType.LT,
                        "<=": TokenType.LE,
                        ">": TokenType.GT,
                        ">=": TokenType.GE,
                    },
                    "STAR": TokenType.STAR,
                    "PLUS": TokenType.PLUS,
                    "MINUS": TokenType.MINUS,
                    "SLASH": TokenType.SLASH,
                    "PERCENT": TokenType.PERCENT,
                    "COMMA": TokenType.COMMA,
                    "LPAREN": TokenType.LPAREN,
                    "RPAREN": TokenType.RPAREN,
                    "LBRACKET": TokenType.LBRACKET,
                    "RBRACKET": TokenType.RBRACKET,
                    "LBRACE": TokenType.LBRACE,
                    "RBRACE": TokenType.RBRACE,
                    "SEMICOLON": TokenType.SEMICOLON,
                    "DOT": TokenType.DOT,
                }
                tokens.append(
                    Token(mapping[kind] if kind != "OP" else mapping[kind][raw], raw, start)
                )
        tokens.append(Token(TokenType.EOF, None, len(text)))
        return tokens
