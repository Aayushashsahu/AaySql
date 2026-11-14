"""Central, exclusive ownership of a file-backed AayushSQL database.

One database file has exactly one legitimate owner: an embedded ``Engine``, a
server, a backup, or a restore. Opening a database can recover the WAL and
rewrite the snapshot, so two independent owners must never coexist.

Design
------
``DatabaseLease`` is the single implementation of that invariant:

* **Exclusive ownership** is taken by an OS-level lock on
  ``<database>.server.lock`` (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
  Windows). The OS drops the lock when the owning process terminates, so a
  crashed owner never blocks a restart and a stale lock *file* is harmless.
* **Same-process exclusion** is provided by ``_REGISTRY``, a thread-safe map
  keyed by the canonical lock-file path. The OS lock alone is sufficient in
  principle, but the registry yields the typed error deterministically and
  documents the invariant in one place.
* **Canonical identity** resolves the parent directory (following symlinks and
  ``..``) and, on Windows, case-folds the path, so ``./app.aaydb`` and
  ``D:/project/app.aaydb`` are the same database. Hard links to one inode under
  different names cannot be detected portably; they are documented as aliases
  the caller must avoid.
* **Borrowing** lets an owner hand its lease to an internal component (server
  -> engine, backup -> engine) without re-acquiring it. A borrow validates that
  the lease is still active, belongs to the same canonical database path, and
  uses that database's canonical lock file. A lease built on some other lock
  path (a *supplemental* lock) can never be borrowed as database ownership.
  Borrowers never release the lease.
* **Canonical ownership is not configurable.** No caller-supplied lock path can
  stand in for ``<database>.server.lock``; an extra lock path is only ever an
  additional coordination lock held *in addition to* canonical ownership.

Ownership is acquired *before* the snapshot is loaded or the WAL is recovered,
and released on every exit path, including construction failures.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

from .errors import DatabaseLockedError, StorageError

#: Suffix of the OS-level lock file. Kept unchanged from v1.0.0 so existing
#: diagnostics and operators keep working.
LOCK_SUFFIX = ".server.lock"

_REGISTRY: dict[str, DatabaseLease] = {}
_REGISTRY_LOCK = threading.Lock()


def canonical_path(path: str | Path) -> Path:
    """Return a stable filesystem identity for ``path`` without requiring it to exist.

    The parent directory is resolved (symlinks, ``..``, separators); the final
    name is kept verbatim except for Windows case folding. Resolving only the
    parent is deliberate: the database file may not exist yet.
    """
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    parent = raw.parent
    try:
        parent = parent.resolve(strict=False)
    except OSError:  # pragma: no cover - defensive for exotic filesystems
        parent = Path(os.path.abspath(str(parent)))
    canonical = parent / raw.name
    if os.name == "nt":
        canonical = Path(os.path.normcase(str(canonical)))
    return canonical


def canonical_lock_path(db_path: str | Path) -> Path:
    """Return the one canonical ownership lock path for ``db_path``.

    This is the only identity that grants database ownership; every file-backed
    entry point (engine, server, backup, restore) must use it.
    """
    return Path(f"{canonical_path(db_path)}{LOCK_SUFFIX}")


class DatabaseLease:
    """An exclusive lease over one file-backed database.

    A lease is *owned* by whoever calls :meth:`acquire` and must be released by
    the same component. Internal components may instead **borrow** an owned,
    active lease through ``Engine(..., _lease=lease)``; borrowers must not
    release it.
    """

    def __init__(
        self,
        lock_path: str | Path,
        *,
        db_path: str | Path | None = None,
        display_path: str | None = None,
    ) -> None:
        self.lock_path = canonical_path(lock_path)
        self.db_path = canonical_path(db_path) if db_path is not None else None
        self.display_path = (
            display_path
            if display_path is not None
            else (str(db_path) if db_path is not None else str(lock_path))
        )
        self._identity = str(self.lock_path)
        self._handle: Any = None

    # -- construction ---------------------------------------------------

    @classmethod
    def for_database(
        cls, db_path: str | Path, *, lock_path: str | Path | None = None
    ) -> DatabaseLease:
        """Build the canonical ownership lease for ``db_path``.

        Passing ``lock_path`` produces a *supplemental* lease: it still carries
        the database identity, but it locks an unrelated file and therefore can
        never be borrowed by an :class:`~aaysql.engine.Engine`. Canonical
        ownership is always acquired separately through
        ``for_database(db_path)``.
        """
        canonical_db = canonical_path(db_path)
        lock = (
            Path(lock_path)
            if lock_path is not None
            else canonical_lock_path(canonical_db)
        )
        return cls(lock, db_path=canonical_db, display_path=str(db_path))

    @classmethod
    def for_lock_path(cls, lock_path: str | Path) -> DatabaseLease:
        """Build a lease from a lock-file path.

        Used by the compatibility ``ServerLock`` wrapper and by callers that
        hold a lock path but no database path. Such a lease cannot be borrowed
        by an ``Engine`` because it carries no canonical database identity.
        """
        return cls(lock_path, display_path=str(lock_path))

    # -- state ----------------------------------------------------------

    @property
    def active(self) -> bool:
        """True while this lease holds the OS lock."""
        return self._handle is not None

    @property
    def is_canonical(self) -> bool:
        """True when this lease *is* the database's ownership identity.

        A lease over a custom lock file, or one built from a bare lock path, is
        never canonical and can never stand in for database ownership.
        """
        return self.db_path is not None and self.lock_path == canonical_lock_path(
            self.db_path
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        state = "active" if self.active else "released"
        return f"DatabaseLease({self.display_path!r}, {state})"

    # -- acquisition ----------------------------------------------------

    def acquire(self) -> None:
        """Take ownership, or raise :class:`DatabaseLockedError`.

        Idempotent for an already-active lease. A failed acquisition leaves no
        registry entry and no open handle behind.
        """
        if self._handle is not None:
            return
        with _REGISTRY_LOCK:
            if self._identity in _REGISTRY:
                raise DatabaseLockedError(self._conflict_message())
            # Reserve the identity before the (potentially slow) file open so
            # two threads cannot both proceed to the OS lock.
            _REGISTRY[self._identity] = self
        try:
            self._open_and_lock()
        except BaseException:
            self._unregister()
            raise

    def _open_and_lock(self) -> None:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a+b")  # noqa: SIM115 - lifetime is the lease
        except OSError as exc:
            raise StorageError(
                f"cannot open database lock file {self.lock_path}: {exc}"
            ) from exc
        try:
            # The lock must be taken before the file contents are touched: on
            # Windows the owner of the byte range is the only writer.
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                handle.close()
            except OSError:
                pass
            raise DatabaseLockedError(self._conflict_message()) from exc
        try:
            # Diagnostic only: the OS lock, never this text, is authoritative.
            handle.seek(0)
            handle.truncate(0)
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
        except OSError:
            pass
        self._handle = handle

    # -- release --------------------------------------------------------

    def release(self) -> None:
        """Release ownership. Idempotent, and safe to call after a failure."""
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    try:
                        handle.close()
                    except OSError:
                        pass
        finally:
            self._unregister()

    def _unregister(self) -> None:
        with _REGISTRY_LOCK:
            if _REGISTRY.get(self._identity) is self:
                del _REGISTRY[self._identity]

    # -- borrowing ------------------------------------------------------

    def validate_borrow(self, db_path: str | Path) -> None:
        """Check that this lease may be lent to an engine for ``db_path``."""
        if not self.active:
            raise StorageError(
                "cannot borrow a database lease that has already been released"
            )
        if self.db_path is None:
            raise StorageError(
                "cannot borrow a lock-path-only lease; acquire it as a database lease"
            )
        expected = canonical_path(db_path)
        if self.db_path != expected:
            raise StorageError(
                f"database lease belongs to '{self.db_path}', not '{expected}'"
            )
        canonical_lock = canonical_lock_path(expected)
        if self.lock_path != canonical_lock:
            raise StorageError(
                f"database lease for '{expected}' uses lock file {self.lock_path}, "
                f"not the canonical ownership lock {canonical_lock}; only the "
                "canonical lease can be borrowed"
            )

    # -- context manager ------------------------------------------------

    def __enter__(self) -> DatabaseLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- diagnostics ----------------------------------------------------

    def _conflict_message(self) -> str:
        return (
            f"database '{self.display_path}' is already in use by another engine or "
            f"process (lock file {self.lock_path}); stop the existing owner before "
            "opening this database"
        )
