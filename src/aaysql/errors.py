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


class AmbiguousCommitError(StorageError):
    """Raised when WAL COMMIT fsync fails, leaving commit durability ambiguous."""
    pass


class DatabaseLockedError(StorageError):
    """A file-backed database is already owned by another engine or process.

    ``code`` is stable and machine-readable; callers should branch on it rather
    than on the message text. Ownership is enforced by an OS-level lock plus a
    process-local registry, so this error is also raised for a second engine in
    the *same* process.
    """

    code = "DATABASE_LOCKED"


