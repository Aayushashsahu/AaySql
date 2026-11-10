from .ast import *
from .errors import ConstraintError, ExecutionError
from .storage import Database, coerce_value


class Executor:
    def __init__(self, db: Database | None = None):
        self.db = db or Database()

    def execute(self, stmt):
        if isinstance(stmt, CreateTable):
            return self.create_table(stmt)
        if isinstance(stmt, DropTable):
            return self.drop_table(stmt)
        if isinstance(stmt, Insert):
            return self.insert(stmt)
        if isinstance(stmt, Select):
            return self.select(stmt)
        if isinstance(stmt, Update):
            return self.update(stmt)
        if isinstance(stmt, Delete):
            return self.delete(stmt)
        raise ExecutionError(f"Unsupported statement: {type(stmt).__name__}")

    def create_table(self, s):
        self.db.create_table(s.name, s.columns)
        return {"status": "ok", "message": f"Table '{s.name}' created"}

    def drop_table(self, s):
        self.db.drop_table(s.name)
        return {"status": "ok", "message": f"Table '{s.name}' dropped"}

    def _coerce(self, value, col):
        return coerce_value(value, col)

    def _build_row(self, table, columns, values):
        if columns is None:
            if len(values) != len(table.columns):
                raise ExecutionError(f"Expected {len(table.columns)} values, got {len(values)}")
            raw = dict(zip(table.column_names, values))
        else:
            if len(columns) != len(values):
                raise ExecutionError(f"Expected {len(columns)} values, got {len(values)}")
            raw = {}
            for name, value in zip(columns, values):
                table.ensure_column(name)
                if name in raw:
                    raise ExecutionError(f"Duplicate column '{name}' in INSERT")
                raw[name] = value
        row = {}
        for col in table.columns:
            value = raw.get(col.name, col.default if col.has_default else None)
            row[col.name] = self._coerce(value, col)
        return row

    def _check_constraints(self, table, candidate, rows=None, ignore_row=None):
        pool = table.rows if rows is None else rows
        for col in table.columns:
            if not (col.primary_key or col.unique):
                continue
            value = candidate[col.name]
            if value is None:
                continue
            for existing in pool:
                if existing is ignore_row:
                    continue
                if existing[col.name] == value:
                    kind = "PRIMARY KEY" if col.primary_key else "UNIQUE"
                    raise ConstraintError(f"{kind} violation on '{col.name}'")

    def insert(self, s):
        table = self.db.table(s.table)
        new_rows = [self._build_row(table, s.columns, values) for values in s.values]
        # Validate all rows against existing rows and earlier rows in the same batch before mutating.
        staged_pool = list(table.rows)
        for row in new_rows:
            self._check_constraints(table, row, rows=staged_pool)
            staged_pool.append(row)
        table.rows.extend(new_rows)
        return {"status": "ok", "rows_affected": len(new_rows)}

    def _value(self, expr, row):
        if isinstance(expr, Literal):
            return expr.value
        if expr is None or isinstance(expr, (str, int, float, bool)):
            return expr
        if isinstance(expr, Identifier):
            if expr.name == "*":
                return row
            if expr.name not in row:
                raise ExecutionError(f"Unknown column '{expr.name}'")
            return row[expr.name]
        if isinstance(expr, UnaryExpr):
            value = self._value(expr.expr, row)
            if expr.op == "NOT":
                return not bool(value)
        if isinstance(expr, BinaryExpr):
            left = self._value(expr.left, row)
            right = self._value(expr.right, row)
            op = expr.op.upper()
            if op == "AND":
                return bool(left) and bool(right)
            if op == "OR":
                return bool(left) or bool(right)
            if left is None or right is None:
                return False
            return self._compare(op, left, right)
        raise ExecutionError(f"Cannot evaluate expression {expr}")

    def _compare(self, op: str, left, right) -> bool:
        if op == "=":
            return left == right
        if op in {"!=", "<>"}:
            return left != right
        try:
            if op == "<":
                return left < right
            if op == "<=":
                return left <= right
            if op == ">":
                return left > right
            if op == ">=":
                return left >= right
        except TypeError as exc:
            raise ExecutionError(
                f"Cannot compare {type(left).__name__} and {type(right).__name__} with operator '{op}'"
            ) from exc
        raise ExecutionError(f"Unsupported operator '{op}'")

    def _filtered(self, table, where):
        if where is None:
            return list(table.rows)
        return [row for row in table.rows if self._value(where, row)]

    def select(self, s):
        table = self.db.table(s.table)
        rows = self._filtered(table, s.where)
        if s.order_by:
            for col, desc in reversed(s.order_by):
                table.ensure_column(col)
                try:
                    rows.sort(key=lambda r: (r[col] is None, "" if r[col] is None else r[col]), reverse=desc)
                except TypeError as exc:
                    raise ExecutionError(f"Cannot sort column '{col}' due to incompatible values") from exc
        if s.limit is not None:
            rows = rows[: s.limit]
        if len(s.columns) == 1 and isinstance(s.columns[0].expr, Identifier) and s.columns[0].expr.name == "*":
            cols = table.column_names
        else:
            cols = [
                item.alias or (item.expr.name if isinstance(item.expr, Identifier) else f"expr_{i+1}")
                for i, item in enumerate(s.columns)
            ]
        data = []
        for row in rows:
            out = {}
            for i, item in enumerate(s.columns):
                value = self._value(item.expr, row)
                if len(s.columns) == 1 and isinstance(item.expr, Identifier) and item.expr.name == "*":
                    out = row.copy()
                    break
                out[cols[i]] = value
            data.append(out)
        return {"status": "ok", "columns": cols, "rows": data, "row_count": len(data)}

    def update(self, s):
        table = self.db.table(s.table)
        targets = self._filtered(table, s.where)
        staged = []
        for old in targets:
            new = old.copy()
            for col, expr in s.assignments:
                table.ensure_column(col)
                col_def = next(c for c in table.columns if c.name == col)
                new[col] = self._coerce(self._value(expr, old), col_def)
            staged.append((old, new))
        target_map = {id(old): new for old, new in staged}
        post_rows = [target_map.get(id(row), row) for row in table.rows]
        for _, new in staged:
            self._check_constraints(table, new, rows=post_rows, ignore_row=new)
        for old, new in staged:
            old.update(new)
        return {"status": "ok", "rows_affected": len(staged)}

    def delete(self, s):
        table = self.db.table(s.table)
        targets = set(map(id, self._filtered(table, s.where)))
        before = len(table.rows)
        table.rows[:] = [row for row in table.rows if id(row) not in targets]
        return {"status": "ok", "rows_affected": before - len(table.rows)}
