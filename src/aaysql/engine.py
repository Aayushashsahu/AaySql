from __future__ import annotations

from pathlib import Path
from typing import Any

from .ast import CreateTable, Delete, DropTable, Insert, Update
from .errors import StorageError
from .executor import Executor
from .lexer import Lexer
from .parser import Parser
from .storage import Database, JsonFileStorage

_MUTATION_STATEMENTS = (CreateTable, DropTable, Insert, Update, Delete)


class Engine:
    """Top-level AayushSQL engine coordinating lexing, parsing, execution, and persistence."""

    def __init__(self, db_path: str | Path | None = None):
        self.lexer = Lexer()
        if db_path is None or str(db_path) == ":memory:":
            self.db_path: Path | None = None
            self.storage: JsonFileStorage | None = None
            self.db = Database()
        else:
            if isinstance(db_path, str) and not db_path.strip():
                raise StorageError("Database path cannot be empty")
            self.db_path = Path(db_path)
            self.storage = JsonFileStorage(self.db_path)
            self.db = self.storage.load()
        self.executor = Executor(self.db)
        self._closed = False

    @property
    def is_persistent(self) -> bool:
        return self.storage is not None

    def execute(self, sql: str) -> dict[str, Any]:
        if self._closed:
            raise StorageError("Cannot execute statement on a closed Engine")
        tokens = self.lexer.tokenize(sql)
        stmt = Parser(tokens).parse()
        if self.storage is not None and isinstance(stmt, _MUTATION_STATEMENTS):
            snapshot = self.db.clone()
            result = self.executor.execute(stmt)
            try:
                self.storage.save(self.db)
            except Exception:
                self.db = snapshot
                self.executor.db = snapshot
                raise
            return result
        return self.executor.execute(stmt)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
