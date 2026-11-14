import time

from .ast import *
from .errors import ConstraintError, ExecutionError
from .planner import ESTIMATE_NOTE, format_metrics, format_plan, plan_select
from .query import (
    SourceBinding,
    eval_row_expr,
    execute_select,
    is_sql_true,
    validate_expr_columns,
)
from .json_value import deep_copy_json, set_path
from .storage import Database, coerce_value
from .undo import (
    CreateIndexUndo,
    CreateTableUndo,
    DeleteUndo,
    DropIndexUndo,
    DropTableUndo,
    InsertUndo,
    UpdateUndo,
)


class Executor:
    def __init__(self, db: Database | None = None, recorder=None, journal=None):
        self.db = db or Database()
        # Optional MutationRecorder; when present every storage-level mutation is
        # appended to it so the engine can write a delta WAL transaction without
        # diffing the database.
        self.recorder = recorder
        # Optional UndoJournal; when present every storage-level mutation first
        # records its exact inverse so a failed statement or transaction can be
        # reversed in memory without cloning the database.
        self.journal = journal

    def _record_undo(self, entry) -> None:
        if self.journal is not None:
            self.journal.record(entry)

    def _record_ddl(self, method, *args) -> None:
        if self.recorder is not None:
            getattr(self.recorder, method)(*args)

    def execute(self, stmt):
        if isinstance(stmt, CreateTable):
            return self.create_table(stmt)
        if isinstance(stmt, CreateCollection):
            return self.create_collection(stmt)
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
        if isinstance(stmt, CreateIndex):
            return self.create_index(stmt)
        if isinstance(stmt, DropIndex):
            return self.drop_index(stmt)
        if isinstance(stmt, Explain):
            return self.explain(stmt)
        raise ExecutionError(f"Unsupported statement: {type(stmt).__name__}")

    def create_collection(self, s):
        self.db.create_collection(s.name)
        self._record_undo(CreateTableUndo(s.name))
        self._record_ddl("record_create_table", self.db.tables[s.name])
        return {"status": "ok", "message": f"Collection '{s.name}' created"}

    def create_table(self, s):
        self.db.create_table(s.name, s.columns)
        self._record_undo(CreateTableUndo(s.name))
        self._record_ddl("record_create_table", self.db.tables[s.name])
        return {"status": "ok", "message": f"Table '{s.name}' created"}

    def drop_table(self, s):
        table = self.db.table(s.name)
        indexes = self.db.indexes_on(s.name)
        self._record_undo(DropTableUndo(s.name, table, indexes))
        self.db.drop_table(s.name)
        self._record_ddl("record_drop_table", s.name)
        return {"status": "ok", "message": f"Table '{s.name}' dropped"}

    def create_index(self, s):
        self.db.create_index(s.name, s.table, s.column)
        self._record_undo(CreateIndexUndo(s.name))
        self._record_ddl("record_create_index", s.name, s.table, s.column)
        return {
            "status": "ok",
            "message": f"Index '{s.name}' created on {s.table}({s.column})",
        }

    def drop_index(self, s):
        if s.name not in self.db.indexes:
            raise ExecutionError(f"Index '{s.name}' does not exist")
        index = self.db.indexes[s.name]
        self._record_undo(DropIndexUndo(s.name, index))
        self.db.drop_index(s.name)
        self._record_ddl("record_drop_index", s.name)
        return {"status": "ok", "message": f"Index '{s.name}' dropped"}

    def explain(self, s):
        if not isinstance(s.statement, Select):
            raise ExecutionError("EXPLAIN currently supports SELECT statements only")
        plan = plan_select(self.db, s.statement)
        text = format_plan(plan, include_note=False)
        if s.analyze:
            # Only EXPLAIN ANALYZE runs the statement; plain EXPLAIN reports the
            # plan from statistics and never executes or mutates anything.
            started = time.perf_counter()
            result = execute_select(self.db, s.statement, plan=plan, collect_metrics=True)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            metrics = dict(result["metrics"])
            metrics["execution_ms"] = elapsed_ms
            text = f"{text}\n{format_metrics(metrics)}"
        text = f"{text}\n\n{ESTIMATE_NOTE}"
        return {"status": "ok", "message": text}

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
        self._record_undo(InsertUndo(table.name, len(table.rows), new_rows))
        table.rows.extend(new_rows)
        table.touch()
        for index in self.db.indexes_on(table.name):
            for row in new_rows:
                index.add(row)
        self._record_ddl("record_insert", table.name, new_rows)
        return {"status": "ok", "rows_affected": len(new_rows)}

    def _value(self, expr, row, table=None):
        if table is None:
            if isinstance(expr, Literal):
                return expr.value
            if expr is None or isinstance(expr, (str, int, float, bool)):
                return expr
            if isinstance(expr, Identifier):
                if expr.name not in row:
                    raise ExecutionError(f"Unknown column '{expr.label}'")
                return row[expr.name]
            raise ExecutionError(f"Cannot evaluate expression {expr}")
        sources = [SourceBinding.from_table(table.name, table)]
        return eval_row_expr(expr, {table.name: row}, sources)

    def _filtered(self, table, where):
        if where is None:
            return list(table.rows)
        sources = [SourceBinding.from_table(table.name, table)]
        validate_expr_columns(where, sources, allow_aggregates=False, context_name="WHERE clause")
        return [
            row for row in table.rows if is_sql_true(eval_row_expr(where, {table.name: row}, sources))
        ]

    def select(self, s):
        return execute_select(self.db, s, plan=plan_select(self.db, s))

    def update(self, s):
        table = self.db.table(s.table)
        sources = [SourceBinding.from_table(table.name, table)]
        targets = self._filtered(table, s.where)
        staged = []
        for item in s.assignments:
            col = item[0]
            expr = item[1]
            table.ensure_column(col)
            validate_expr_columns(expr, sources, allow_aggregates=False, context_name="UPDATE assignment")
        for old in targets:
            new = deep_copy_json(old)
            for item in s.assignments:
                col = item[0]
                expr = item[1]
                path = item[2] if len(item) == 3 else ()
                col_def = next(c for c in table.columns if c.name == col)
                if path:
                    old_doc = new[col]
                    val = eval_row_expr(expr, {table.name: old}, sources)
                    new[col] = self._coerce(set_path(old_doc, path, val), col_def)
                else:
                    new[col] = self._coerce(eval_row_expr(expr, {table.name: old}, sources), col_def)
            staged.append((old, new))
        target_map = {id(old): new for old, new in staged}
        post_rows = [target_map.get(id(row), row) for row in table.rows]
        for _, new in staged:
            self._check_constraints(table, new, rows=post_rows, ignore_row=new)
        indexes = self.db.indexes_on(table.name)
        positions = {id(row): pos for pos, row in enumerate(table.rows)}
        previous_keys = [
            {index.name: old[index.column] for index in indexes} for old, _ in staged
        ]
        for old, _new in staged:
            self._record_undo(
                UpdateUndo(table.name, positions[id(old)], deep_copy_json(old))
            )
        for old, new in staged:
            old.update(new)
        if staged:
            table.touch()
            if self.recorder is not None:
                for old, _ in staged:
                    self._record_ddl(
                        "record_update", table.name, positions[id(old)], old
                    )
        for (old, _), previous in zip(staged, previous_keys):
            for index in indexes:
                index.remove(old, key=previous[index.name], forget=False)
                index.add(old)
        return {"status": "ok", "rows_affected": len(staged)}

    def delete(self, s):
        table = self.db.table(s.table)
        targets = set(map(id, self._filtered(table, s.where)))
        doomed = [row for row in table.rows if id(row) in targets]
        before = len(table.rows)
        positions = {id(row): pos for pos, row in enumerate(table.rows)}
        if doomed:
            # Record inverses before mutating, so a failure during index
            # maintenance still leaves enough information to restore the rows.
            self._record_undo(
                DeleteUndo(
                    table.name,
                    [(positions[id(row)], row) for row in doomed],
                )
            )
        table.rows[:] = [row for row in table.rows if id(row) not in targets]
        if len(table.rows) != before:
            table.touch()
        for index in self.db.indexes_on(table.name):
            for row in doomed:
                index.remove(row)
        if len(table.rows) != before and self.recorder is not None:
            doomed_positions = [
                positions[id(row)] for row in doomed if id(row) in positions
            ]
            self._record_ddl("record_delete", table.name, doomed_positions)
        return {"status": "ok", "rows_affected": before - len(table.rows)}
