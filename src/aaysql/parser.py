from .ast import *
from .errors import ParserError
from .storage import SUPPORTED_TYPES, TYPE_MAP, validate_column_def
from .token import Token, TokenType


class Parser:
    def __init__(self, tokens: list[Token]):
        self.t = tokens
        self.i = 0

    def current(self) -> Token:
        return self.t[min(self.i, len(self.t) - 1)]

    def peek(self, n=1) -> Token:
        return self.t[min(self.i + n, len(self.t) - 1)]

    def advance(self) -> Token:
        token = self.current()
        self.i += 1
        return token

    def match(self, typ: TokenType, value: str | None = None) -> bool:
        tok = self.current()
        if tok.type != typ:
            return False
        return value is None or str(tok.value).upper() == value.upper()

    def expect(self, typ: TokenType, value: str | None = None) -> Token:
        if not self.match(typ, value):
            want = value or typ.name
            got = self.current().value if self.current().value is not None else self.current().type.name
            raise ParserError(f"Expected {want}, got {got!r} at position {self.current().position}")
        return self.advance()

    def optional_semicolon(self):
        if self.match(TokenType.SEMICOLON):
            self.advance()
        if not self.match(TokenType.EOF):
            raise ParserError(f"Unexpected token {self.current().value!r} at position {self.current().position}")

    def parse(self):
        self.expect(TokenType.PREFIX, "AAYUSH")
        if self.match(TokenType.PREFIX):
            raise ParserError("Duplicate AAYUSH prefix. One AAYUSH is sufficient.")
        stmt = self.parse_statement()
        self.optional_semicolon()
        return stmt

    def keyword(self, word: str) -> bool:
        return self.match(TokenType.IDENT, word)

    def expect_kw(self, word: str):
        self.expect(TokenType.IDENT, word)

    def ident(self) -> str:
        return str(self.expect(TokenType.IDENT).value)

    def parse_statement(self):
        if self.keyword("CREATE"):
            return self.parse_create()
        if self.keyword("DROP"):
            return self.parse_drop()
        if self.keyword("INSERT"):
            return self.parse_insert()
        if self.keyword("SELECT"):
            return self.parse_select()
        if self.keyword("UPDATE"):
            return self.parse_update()
        if self.keyword("DELETE"):
            return self.parse_delete()
        raise ParserError(f"Unknown statement {self.current().value!r}")

    def parse_create(self):
        self.expect_kw("CREATE")
        self.expect_kw("TABLE")
        name = self.ident()
        self.expect(TokenType.LPAREN)
        cols = []
        while True:
            col_name = self.ident()
            type_tok = self.expect(TokenType.IDENT)
            type_name = str(type_tok.value).upper()
            if type_name not in TYPE_MAP:
                raise ParserError(
                    f"Unsupported data type '{type_tok.value}' at position {type_tok.position}. "
                    f"Supported types: {', '.join(SUPPORTED_TYPES)}"
                )
            primary = not_null = unique = has_default = False
            default = None
            while self.current().type == TokenType.IDENT:
                kw_tok = self.current()
                kw = str(kw_tok.value).upper()
                if kw == "PRIMARY":
                    self.advance(); self.expect_kw("KEY"); primary = True
                elif kw == "NOT":
                    self.advance(); self.expect_kw("NULL"); not_null = True
                elif kw == "UNIQUE":
                    self.advance(); unique = True
                elif kw == "DEFAULT":
                    if has_default:
                        raise ParserError(
                            f"Duplicate DEFAULT constraint for column '{col_name}' at position {kw_tok.position}"
                        )
                    self.advance(); default = self.parse_literal().value; has_default = True
                else:
                    break
            col_def = validate_column_def(
                ColumnDef(col_name, type_name, primary, not_null, unique, default, has_default)
            )
            cols.append(col_def)
            if not self.match(TokenType.COMMA):
                break
            self.advance()
        self.expect(TokenType.RPAREN)
        return CreateTable(name, cols)

    def parse_drop(self):
        self.expect_kw("DROP"); self.expect_kw("TABLE")
        return DropTable(self.ident())

    def parse_insert(self):
        self.expect_kw("INSERT"); self.expect_kw("INTO")
        table = self.ident()
        columns = None
        if self.match(TokenType.LPAREN):
            self.advance(); columns = [self.ident()]
            while self.match(TokenType.COMMA):
                self.advance(); columns.append(self.ident())
            self.expect(TokenType.RPAREN)
        self.expect_kw("VALUES")
        values = []
        while True:
            self.expect(TokenType.LPAREN)
            row = [self.parse_literal().value]
            while self.match(TokenType.COMMA):
                self.advance(); row.append(self.parse_literal().value)
            self.expect(TokenType.RPAREN)
            values.append(row)
            if not self.match(TokenType.COMMA):
                break
            self.advance()
        return Insert(table, columns, values)

    def parse_select(self):
        self.expect_kw("SELECT")
        items = []
        if self.match(TokenType.STAR):
            self.advance(); items.append(SelectItem(Identifier("*")))
        else:
            items.append(self.parse_select_item())
            while self.match(TokenType.COMMA):
                self.advance(); items.append(self.parse_select_item())
        self.expect_kw("FROM")
        table = self.ident()
        where = None
        if self.keyword("WHERE"):
            self.advance(); where = self.parse_expr()
        order_by = None
        if self.keyword("ORDER"):
            self.advance(); self.expect_kw("BY")
            order_by = [(self.ident(), False)]
            if self.keyword("ASC"):
                self.advance()
            elif self.keyword("DESC"):
                self.advance(); order_by = [(order_by[0][0], True)]
            while self.match(TokenType.COMMA):
                self.advance(); col = self.ident(); desc = False
                if self.keyword("ASC"): self.advance()
                elif self.keyword("DESC"): self.advance(); desc = True
                order_by.append((col, desc))
        limit = None
        if self.keyword("LIMIT"):
            self.advance(); tok = self.expect(TokenType.NUMBER)
            if not isinstance(tok.value, int) or tok.value < 0: raise ParserError("LIMIT must be a non-negative integer")
            limit = tok.value
        return Select(items, table, where, order_by, limit)

    def parse_select_item(self):
        expr = self.parse_primary()
        alias = None
        if self.keyword("AS"):
            self.advance(); alias = self.ident()
        return SelectItem(expr, alias)

    def parse_update(self):
        self.expect_kw("UPDATE"); table = self.ident(); self.expect_kw("SET")
        assignments = [self.parse_assignment()]
        while self.match(TokenType.COMMA):
            self.advance()
            assignments.append(self.parse_assignment())
        where = None
        if self.keyword("WHERE"):
            self.advance(); where = self.parse_expr()
        return Update(table, assignments, where)

    def parse_assignment(self):
        col = self.ident()
        self.expect(TokenType.EQ, "=")
        return col, self.parse_assignment_value()

    def parse_assignment_value(self):
        tok = self.current()
        if self.match(TokenType.EQ):
            raise ParserError(f"Unexpected '=' at position {tok.position}")
        if self.keyword("WHERE"):
            raise ParserError(f"Expected assignment value, got 'WHERE' at position {tok.position}")
        if tok.type in {TokenType.STRING, TokenType.NUMBER} or self.keyword("NULL"):
            return self.parse_literal().value
        if tok.type == TokenType.IDENT or self.match(TokenType.LPAREN):
            return self.parse_primary()
        got = tok.value if tok.value is not None else tok.type.name
        raise ParserError(f"Expected assignment value, got {got!r} at position {tok.position}")

    def parse_delete(self):
        self.expect_kw("DELETE"); self.expect_kw("FROM"); table = self.ident()
        where = None
        if self.keyword("WHERE"):
            self.advance(); where = self.parse_expr()
        return Delete(table, where)

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.keyword("OR"):
            self.advance(); left = BinaryExpr(left, "OR", self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.keyword("AND"):
            self.advance(); left = BinaryExpr(left, "AND", self.parse_not())
        return left

    def parse_not(self):
        if self.keyword("NOT"):
            self.advance(); return UnaryExpr("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_primary()
        if self.current().type in {TokenType.EQ, TokenType.NE, TokenType.LT, TokenType.LE, TokenType.GT, TokenType.GE}:
            op = str(self.advance().value)
            return BinaryExpr(left, op, self.parse_primary())
        return left

    def parse_primary(self):
        if self.match(TokenType.LPAREN):
            self.advance(); expr = self.parse_expr(); self.expect(TokenType.RPAREN); return expr
        if self.current().type == TokenType.IDENT:
            return Identifier(self.advance().value)
        return self.parse_literal()

    def parse_literal(self):
        tok = self.current()
        if tok.type in {TokenType.STRING, TokenType.NUMBER}:
            self.advance(); return Literal(tok.value)
        if self.keyword("NULL"):
            self.advance(); return Literal(None)
        got = tok.value if tok.value is not None else tok.type.name
        raise ParserError(f"Expected literal, got {got!r} at position {tok.position}")
