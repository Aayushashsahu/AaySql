class AayushSQLError(Exception):
    """Base exception for AayushSQL."""


class LexerError(AayushSQLError):
    pass


class ParserError(AayushSQLError):
    pass


class ExecutionError(AayushSQLError):
    pass


class ConstraintError(ExecutionError):
    pass


class StorageError(AayushSQLError):
    """Raised when loading, validating, or saving persistent database state fails."""
    pass

