"""AayushSQL database server: one engine, many connections, serialized execution.

Architecture in one paragraph
-----------------------------
The server owns exactly one :class:`~aaysql.engine.Engine` for its database. Every
client connection is handled by its own thread, but *all* engine access passes
through :class:`TransactionCoordinator`, which holds a single execution lock and
tracks which session owns the one active transaction. Multiple clients therefore
get independent connections and session-owned transactions, while database work
stays strictly serialized. See ``SERVER_ARCHITECTURE.md`` for the full contract.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from . import __version__
from .engine import Engine
from .errors import (
    AmbiguousCommitError,
    ConstraintError,
    ExecutionError,
    LexerError,
    ParserError,
    StorageError,
)
from .protocol import (
    AUTH_TOKEN_ENV,
    AUTHENTICATION_FAILED,
    CONNECTION_TIMEOUT,
    CONSTRAINT_ERROR,
    DATABASE_UNAVAILABLE,
    DEFAULT_HOST,
    DEFAULT_MAX_FRAME_SIZE,
    DEFAULT_PORT,
    HELLO_REQUIRED,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PROTOCOL_VERSION,
    RECOVERY_REQUIRED,
    SQL_EXECUTION_ERROR,
    SQL_PARSE_ERROR,
    TOO_MANY_CONNECTIONS,
    TRANSACTION_BUSY,
    TRANSACTION_ERROR,
    FrameError,
    FrameReader,
    decode_message,
    error_response,
    hello_result,
    ok_response,
    send_frame,
    validate_envelope,
    validate_sql_operation,
)

LOGGER = logging.getLogger("aaysql.server")

#: Suffix of the OS-level lock file that marks "this database has a server".
LOCK_SUFFIX = ".server.lock"
DEFAULT_DB_PATH = "aaysql.aaydb"
RECV_CHUNK = 65536
MAX_RECENT_REQUEST_IDS = 256


# --- configuration --------------------------------------------------------


class ServerConfigError(Exception):
    """Invalid server configuration; raised before anything is bound or opened."""


@dataclass
class ServerConfig:
    """Explicit, validated server configuration.

    The token is never accepted from the command line; pass it through
    ``AAYUSQL_AUTH_TOKEN`` (or this field programmatically).
    """

    db_path: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auth_token: str | None = None
    auth_max_attempts: int = 3
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_client_ca: str | None = None
    max_frame_size: int = DEFAULT_MAX_FRAME_SIZE
    max_clients: int = 32
    idle_timeout: float = 300.0
    read_timeout: float = 60.0
    write_timeout: float = 30.0
    shutdown_timeout: float = 10.0
    lock_path: Path | None = None

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    @property
    def in_memory(self) -> bool:
        return self.db_path is None or str(self.db_path) == ":memory:"

    @property
    def is_loopback(self) -> bool:
        host = (self.host or "").strip().lower()
        if host in {"localhost"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @property
    def effective_lock_path(self) -> Path | None:
        if self.in_memory:
            return None
        if self.lock_path is not None:
            return Path(self.lock_path)
        return Path(f"{self.db_path}{LOCK_SUFFIX}")

    def validate(self) -> None:
        """Reject unsafe or contradictory configurations before startup."""
        if not isinstance(self.host, str) or not self.host.strip():
            raise ServerConfigError("host must be a non-empty string")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ServerConfigError("port must be an integer")
        if not 0 <= self.port <= 65535:
            raise ServerConfigError("port must be between 0 and 65535")
        if not 1024 <= self.max_frame_size <= 64 * 1024 * 1024:
            raise ServerConfigError("max_frame_size must be between 1024 and 67108864 bytes")
        if isinstance(self.max_clients, bool) or not isinstance(self.max_clients, int):
            raise ServerConfigError("max_clients must be an integer")
        if self.max_clients < 1:
            raise ServerConfigError("max_clients must be at least 1")
        if self.auth_max_attempts < 1:
            raise ServerConfigError("auth_max_attempts must be at least 1")
        for name in ("idle_timeout", "read_timeout", "write_timeout", "shutdown_timeout"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ServerConfigError(f"{name} must be a positive number of seconds")
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ServerConfigError("TLS requires both tls_cert and tls_key")
        if self.tls_client_ca is not None and not self.tls_enabled:
            raise ServerConfigError("tls_client_ca requires tls_cert and tls_key")
        if self.auth_token is not None and not self.auth_token.strip():
            raise ServerConfigError("auth_token must not be empty when configured")
        if self.tls_enabled:
            for label, value in (("tls_cert", self.tls_cert), ("tls_key", self.tls_key)):
                if not value or not Path(value).is_file():
                    raise ServerConfigError(f"{label} file not found: {value!r}")
            if self.tls_client_ca is not None and not Path(self.tls_client_ca).is_file():
                raise ServerConfigError(f"tls_client_ca file not found: {self.tls_client_ca!r}")
        if not self.is_loopback:
            # A local database server must never become publicly reachable by
            # accident: non-loopback binding requires authentication *and* TLS.
            if not self.auth_token:
                raise ServerConfigError(
                    f"refusing to bind to non-loopback host {self.host!r} without "
                    f"authentication; set {AUTH_TOKEN_ENV}"
                )
            if not self.tls_enabled:
                raise ServerConfigError(
                    f"refusing to bind to non-loopback host {self.host!r} without TLS; "
                    "configure --tls-cert and --tls-key"
                )

    @classmethod
    def from_environment(cls, **kwargs: Any) -> ServerConfig:
        """Build a config, reading the auth token from the environment only.

        The environment takes precedence over an explicitly supplied token; the
        explicit one is popped first so passing both does not raise a duplicate
        keyword error.
        """
        explicit_token = kwargs.pop("auth_token", None)
        token = os.environ.get(AUTH_TOKEN_ENV)
        if token is not None and not token.strip():
            raise ServerConfigError(f"{AUTH_TOKEN_ENV} is set but empty; unset it or provide a token")
        return cls(auth_token=token if token is not None else explicit_token, **kwargs)


# --- OS-level ownership ---------------------------------------------------


class ServerLock:
    """Exclusive per-database lock, released by the OS if the process dies.

    POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` on one byte.
    No stale-PID heuristics are involved: the operating system drops the lock
    when the owning process terminates, so a crashed server never blocks a
    restart.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._handle: Any = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")  # noqa: SIM115 - lifetime is the lock
        except OSError as exc:
            raise ServerConfigError(f"cannot open server lock file {self.path}: {exc}") from exc
        try:
            # Take the OS lock before touching the file contents: on Windows the
            # holding process owns the locked byte range, so a second server may
            # not write (or even truncate) it.
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ServerConfigError(
                f"database is already owned by another AayushSQL server "
                f"(lock file {self.path}): {exc}"
            ) from exc
        # The pid is diagnostic only; the lock itself is the authority.
        try:
            handle.seek(0)
            handle.truncate(0)
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
        except OSError:
            pass
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
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

    def __enter__(self) -> ServerLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


# --- sessions and coordination -------------------------------------------


class ServerRequestError(Exception):
    """A request-level failure that maps straight onto a protocol error code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class Session:
    """State owned by one accepted connection. Ids are always server-generated."""

    session_id: str
    peer: tuple[str, int] | None
    connected_at: float
    last_activity: float
    socket: Any = field(default=None, repr=False)
    protocol_version: int | None = None
    hello_done: bool = False
    authenticated: bool = False
    requests: int = 0
    auth_failures: int = 0
    owns_transaction: bool = False
    client_name: str = "unknown"
    client_version: str = "unknown"
    recent_ids: set[str] = field(default_factory=set)
    recent_order: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_REQUEST_IDS))

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def remember_request(self, request_id: str) -> bool:
        """Track recent request ids; returns False for a duplicate."""
        if request_id in self.recent_ids:
            return False
        if len(self.recent_order) == self.recent_order.maxlen:
            evicted = self.recent_order[0]
            self.recent_ids.discard(evicted)
        self.recent_order.append(request_id)
        self.recent_ids.add(request_id)
        return True


class TransactionCoordinator:
    """Serializes engine access and enforces single-session transaction ownership.

    The engine has exactly one ``TransactionManager``; this class decides which
    session may use it. Every engine call happens under ``self._lock`` and no
    response is written while that lock is held.
    """

    def __init__(self, engine: Engine, *, logger: logging.Logger) -> None:
        self.engine = engine
        self._logger = logger
        self._lock = threading.Lock()
        self._owner: str | None = None
        self._unavailable_reason: str | None = None
        self._shut_down = False

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def _mark_unavailable(self, reason: str) -> None:
        if self._unavailable_reason is None:
            self._unavailable_reason = reason
            self._logger.error("database service marked unavailable: %s", reason)

    def _assert_available(self) -> None:
        if self._shut_down:
            raise ServerRequestError(DATABASE_UNAVAILABLE, "server is shutting down")
        if self._unavailable_reason is not None:
            raise ServerRequestError(RECOVERY_REQUIRED, self._unavailable_reason)
        if self.engine.needs_recovery or self.engine.wal_poisoned:
            self._mark_unavailable(
                "database durability state is ambiguous after a WAL failure; "
                "reopen the server to recover"
            )
            raise ServerRequestError(RECOVERY_REQUIRED, "database requires recovery; reopen the server")

    def execute(self, session: Session, sql: str) -> dict[str, Any]:
        """Run one statement for ``session`` under the execution lock."""
        with self._lock:
            self._assert_available()
            if self._owner is not None and self._owner != session.session_id:
                raise ServerRequestError(
                    TRANSACTION_BUSY,
                    "another client owns the active transaction; retry after it "
                    "commits or rolls back",
                    retryable=True,
                )
            try:
                result = self.engine.execute(sql)
            except AmbiguousCommitError:
                self._mark_unavailable(
                    "commit durability is ambiguous after a WAL fsync failure; "
                    "the database must be recovered by reopening it"
                )
                raise
            except Exception:
                if self.engine.needs_recovery or self.engine.wal_poisoned:
                    self._mark_unavailable(
                        "database durability state is ambiguous after a WAL failure; "
                        "reopen the server to recover"
                    )
                raise
            # The engine exposes the authoritative transaction state; ownership
            # follows it rather than guessing from the SQL text.
            if self.engine.in_transaction:
                self._owner = session.session_id
                session.owns_transaction = True
            else:
                self._owner = None
                session.owns_transaction = False
            return result

    def release_session(self, session: Session) -> str:
        """Roll back a disconnected owner's transaction. Returns a short outcome."""
        with self._lock:
            if self._owner != session.session_id:
                return "no_transaction"
            if self._unavailable_reason is not None or self.engine.needs_recovery:
                self._logger.error(
                    "session %s disconnected after an ambiguous commit; not rolling back "
                    "and not releasing the database for further use",
                    session.session_id,
                )
                return "recovery_required"
            try:
                if self.engine.in_transaction:
                    self.engine.rollback()
                    self._logger.info(
                        "rolled back the uncommitted transaction of disconnected session %s",
                        session.session_id,
                    )
                    return "rolled_back"
                return "no_transaction"
            except Exception:
                self._logger.exception(
                    "rollback for disconnected session %s failed", session.session_id
                )
                self._mark_unavailable(
                    "could not roll back a disconnected client's transaction; "
                    "reopen the server to recover"
                )
                return "failed"
            finally:
                if not self.engine.in_transaction:
                    self._owner = None
                    session.owns_transaction = False

    def shutdown(self, timeout: float) -> bool:
        """Bounded wait for in-flight work, then roll back and close the engine."""
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            self._logger.error(
                "in-flight database work did not finish within %.1fs; "
                "closing without touching the engine",
                timeout,
            )
            self._shut_down = True
            return False
        try:
            self._shut_down = True
            if self._unavailable_reason is not None or self.engine.needs_recovery:
                self._logger.error(
                    "closing the engine with an ambiguous durability state; "
                    "the WAL is preserved for reopen recovery"
                )
            elif self.engine.in_transaction:
                try:
                    self.engine.rollback()
                    self._logger.info("rolled back an uncommitted transaction during shutdown")
                except Exception:
                    self._logger.exception("rollback during shutdown failed")
                    self._mark_unavailable("shutdown could not roll back an active transaction")
            try:
                self.engine.close()
            except Exception:
                self._logger.exception("engine close during shutdown failed")
            return True
        finally:
            self._lock.release()


# --- TCP plumbing ---------------------------------------------------------


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64
    owner: Any = None

    def process_request(self, request: Any, client_address: Any) -> None:
        owner = self.owner
        if not owner._try_acquire_slot():
            owner._connections_rejected += 1
            owner._logger.warning(
                "rejecting connection from %s: maximum of %d clients reached",
                _peer_label(client_address),
                owner.config.max_clients,
            )
            # Reject off the accept thread so a slow peer cannot stall accepts.
            threading.Thread(
                target=_reject_connection,
                args=(
                    request,
                    error_response(
                        None,
                        TOO_MANY_CONNECTIONS,
                        "server is at its connection limit; retry later",
                    ),
                    owner.config.max_frame_size,
                ),
                name="aaysql-reject",
                daemon=True,
            ).start()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            owner._release_client_slot()
            raise


class _ClientHandler(socketserver.BaseRequestHandler):
    """Reads framed requests from one connection until it closes."""

    server: _ThreadingTCPServer

    def handle(self) -> None:
        owner = self.server.owner
        session = owner._new_session(self.request, self.client_address)
        try:
            self._serve(owner, session)
        finally:
            owner._close_session(session)

    def finish(self) -> None:
        self.server.owner._release_client_slot()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver would print a traceback to stderr; log it server-side.
        LOGGER.exception(
            "unhandled error while serving client %s", _peer_label(client_address)
        )

    def _serve(self, owner: AayushSQLServer, session: Session) -> None:
        sock = self.request
        reader = FrameReader(owner.config.max_frame_size)
        sock.settimeout(owner.config.read_timeout)
        while not owner.stopping:
            try:
                chunk = sock.recv(RECV_CHUNK)
            except TimeoutError:
                if reader.has_partial:
                    owner._send_safely(
                        sock,
                        error_response(
                            None,
                            CONNECTION_TIMEOUT,
                            "timed out while reading a request",
                        ),
                    )
                    owner._logger.info(
                        "closing session %s: request read timeout with %d buffered bytes",
                        session.session_id,
                        reader.buffered,
                    )
                    return
                if time.monotonic() - session.last_activity >= owner.config.idle_timeout:
                    owner._logger.info(
                        "closing session %s after idle timeout", session.session_id
                    )
                    owner._send_safely(
                        sock,
                        error_response(None, CONNECTION_TIMEOUT, "connection idle timeout"),
                    )
                    return
                continue
            except OSError:
                return
            if not chunk:
                if reader.has_partial:
                    owner._logger.info(
                        "session %s closed mid-frame (%d buffered bytes)",
                        session.session_id,
                        reader.buffered,
                    )
                return
            session.touch()
            reader.feed(chunk)
            while True:
                try:
                    payload = reader.pop()
                except FrameError as exc:
                    # The stream is no longer trustworthy: answer and close.
                    owner._send_safely(sock, error_response(None, exc.code, exc.message))
                    owner._logger.warning(
                        "session %s: %s (%s)", session.session_id, exc.code, exc.message
                    )
                    return
                if payload is None:
                    break
                try:
                    message = decode_message(payload)
                except FrameError as exc:
                    if not owner._send_safely(
                        sock, error_response(None, exc.code, exc.message)
                    ):
                        return
                    owner._logger.info(
                        "session %s sent an undecodable payload: %s",
                        session.session_id,
                        exc.code,
                    )
                    continue
                response, close = owner._dispatch(message, session)
                sock.settimeout(owner.config.write_timeout)
                delivered = owner._send_safely(sock, response)
                sock.settimeout(owner.config.read_timeout)
                if not delivered or close:
                    return


def _reject_connection(
    sock: Any, payload: dict[str, Any], max_frame_size: int, timeout: float = 1.0
) -> None:
    """Send a final error frame and close without resetting on the peer.

    Closing a socket that still has unsent data can make the kernel send RST,
    which would destroy the error frame the client is meant to read. Half-close,
    drain briefly, then close.
    """
    try:
        send_frame(sock, payload, max_frame_size)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.settimeout(timeout)
        while sock.recv(RECV_CHUNK):
            pass
    except (OSError, FrameError):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _peer_label(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def is_transaction_statement(sql: str) -> bool:
    """True when ``sql`` is a transaction-control statement.

    Used only to pick a more precise error code; transaction *state* always comes
    from the engine, never from parsing SQL.
    """
    words = sql.replace("(", " ").replace(")", " ").split()
    if words and words[0].upper() == "AAYUSH":
        words = words[1:]
    return bool(words) and words[0].upper() in {"BEGIN", "COMMIT", "ROLLBACK"}


def _sanitize(value: Any, limit: int = 40) -> str:
    """Make a client-supplied label safe for logs."""
    if not isinstance(value, str):
        return "unknown"
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:limit] or "unknown"


# --- server ---------------------------------------------------------------


class AayushSQLServer:
    """Owns one engine and serves framed SQL requests over TCP."""

    def __init__(
        self,
        config: ServerConfig | None = None,
        *,
        engine: Engine | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or ServerConfig()
        self.config.validate()
        self._logger = logger or LOGGER
        self._injected_engine = engine
        self._engine: Engine | None = None
        self._lock: ServerLock | None = None
        self._coordinator: TransactionCoordinator | None = None
        self._tcp: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = threading.Lock()
        self._slots: threading.BoundedSemaphore | None = None
        self._started = False
        self._address: tuple[str, int] | None = None
        self._started_at: float | None = None
        self._requests_served = 0
        self._connections_accepted = 0
        self._connections_rejected = 0

    # -- introspection -------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise ServerConfigError("server has not been started")
        return self._address

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise ServerConfigError("server has not been started")
        return self._engine

    @property
    def coordinator(self) -> TransactionCoordinator:
        if self._coordinator is None:
            raise ServerConfigError("server has not been started")
        return self._coordinator

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    @property
    def session_count(self) -> int:
        with self._sessions_lock:
            return len(self._sessions)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "requests_served": self._requests_served,
            "connections_accepted": self._connections_accepted,
            "connections_rejected": self._connections_rejected,
            "active_sessions": self.session_count,
            "transaction_owner": self._coordinator.owner if self._coordinator else None,
            "unavailable_reason": (
                self._coordinator.unavailable_reason if self._coordinator else None
            ),
        }

    # -- lifecycle -----------------------------------------------------

    def start(self) -> tuple[str, int]:
        """Validate, lock, open the engine, bind, and start accepting."""
        if self._started:
            return self.address
        self.config.validate()
        lock = None
        engine = self._injected_engine
        created_engine = False
        try:
            if self.config.effective_lock_path is not None:
                lock = ServerLock(self.config.effective_lock_path)
                lock.acquire()
            if engine is None:
                engine = Engine(self.config.db_path)
                created_engine = True
            coordinator = TransactionCoordinator(engine, logger=self._logger)
            tcp = _ThreadingTCPServer(
                (self.config.host, self.config.port),
                _ClientHandler,
                bind_and_activate=False,
            )
            tcp.owner = self
            tcp.server_bind()
            if self.config.tls_enabled:
                tcp.socket = self._build_tls_context().wrap_socket(
                    tcp.socket, server_side=True
                )
            tcp.server_activate()
        except BaseException:
            if lock is not None:
                lock.release()
            if created_engine and engine is not None:
                try:
                    engine.close()
                except Exception as exc:  # noqa: BLE001 - startup is being torn down
                    LOGGER.debug("engine close during failed startup ignored: %s", exc)
            raise

        self._lock = lock
        self._engine = engine
        self._coordinator = coordinator
        self._tcp = tcp
        self._slots = threading.BoundedSemaphore(self.config.max_clients)
        self._address = (tcp.server_address[0], tcp.server_address[1])
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._serve_forever, name="aaysql-server", daemon=True
        )
        self._thread.start()
        self._started = True
        self._logger.info(
            "AayushSQL v%s listening on %s:%s (protocol %d, db=%s, tls=%s, auth=%s, max_clients=%d)",
            __version__,
            self._address[0],
            self._address[1],
            PROTOCOL_VERSION,
            "memory" if self.config.in_memory else self.config.db_path,
            "on" if self.config.tls_enabled else "off",
            "on" if self.config.auth_token else "off",
            self.config.max_clients,
        )
        return self.address

    def _serve_forever(self) -> None:
        assert self._tcp is not None
        try:
            self._tcp.serve_forever(poll_interval=0.2)
        except Exception:
            if not self.stopping:
                self._logger.exception("accept loop terminated unexpectedly")

    def serve_forever(self) -> None:
        """Block until :meth:`shutdown` is called (CLI behaviour)."""
        self.start()
        while not self._stopping.wait(0.2):
            pass

    def shutdown(self) -> bool:
        """Graceful shutdown. Returns True when in-flight work was drained."""
        with self._shutdown_lock:
            if self._stopping.is_set():
                return True
            self._stopping.set()
        self._logger.info("shutdown requested")
        tcp = self._tcp
        if tcp is not None:
            try:
                tcp.shutdown()
            except Exception as exc:  # noqa: BLE001 - shutting down a failed listener
                self._logger.debug("listener shutdown ignored: %s", exc)
            try:
                tcp.server_close()
            except OSError:
                pass
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            sock = session.socket
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # Let handlers unwind their sessions (rollback included) before the engine
        # is closed underneath them. The whole shutdown shares one deadline.
        deadline = time.monotonic() + self.config.shutdown_timeout
        while self.session_count and time.monotonic() < deadline:
            time.sleep(0.01)
        remaining = max(deadline - time.monotonic(), 0.0)
        drained = True
        if self._coordinator is not None:
            drained = self._coordinator.shutdown(remaining)
        if self._thread is not None:
            self._thread.join(timeout=max(deadline - time.monotonic(), 0.0))
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        self._started = False
        if drained:
            self._logger.info(
                "shutdown complete (requests=%d, connections=%d)",
                self._requests_served,
                self._connections_accepted,
            )
        else:
            self._logger.error("shutdown finished without draining in-flight work")
        return drained

    def _build_tls_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        assert self.config.tls_cert and self.config.tls_key
        context.load_cert_chain(self.config.tls_cert, self.config.tls_key)
        if self.config.tls_client_ca:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cafile=self.config.tls_client_ca)
        return context

    # -- connection bookkeeping ----------------------------------------

    def _try_acquire_slot(self) -> bool:
        assert self._slots is not None
        return self._slots.acquire(blocking=False)

    def _release_client_slot(self) -> None:
        if self._slots is None:
            return
        try:
            self._slots.release()
        except ValueError:
            pass

    def _new_session(self, sock: Any, peer: Any) -> Session:
        now = time.monotonic()
        session = Session(
            session_id=uuid.uuid4().hex,
            peer=peer if isinstance(peer, tuple) and len(peer) >= 2 else None,
            connected_at=now,
            last_activity=now,
            socket=sock,
        )
        with self._sessions_lock:
            self._sessions[session.session_id] = session
            self._connections_accepted += 1
        self._logger.info(
            "connection accepted from %s (session %s)",
            _peer_label(peer),
            session.session_id,
        )
        return session

    def _close_session(self, session: Session) -> None:
        outcome = "no_transaction"
        if self._coordinator is not None:
            try:
                outcome = self._coordinator.release_session(session)
            except Exception:
                self._logger.exception("session cleanup failed for %s", session.session_id)
        with self._sessions_lock:
            self._sessions.pop(session.session_id, None)
        self._logger.info(
            "connection closed session=%s client=%s/%s requests=%d transaction=%s",
            session.session_id,
            session.client_name,
            session.client_version,
            session.requests,
            outcome,
        )
        session.socket = None

    # -- request handling ----------------------------------------------

    def _send_safely(self, sock: Any, response: dict[str, Any]) -> bool:
        try:
            send_frame(sock, response, self.config.max_frame_size)
            return True
        except FrameError as exc:
            try:
                send_frame(
                    sock,
                    error_response(
                        response.get("request_id"),
                        exc.code,
                        "response exceeded the maximum frame size",
                    ),
                    self.config.max_frame_size,
                )
            except Exception:
                return False
            return False
        except OSError:
            return False

    def _dispatch(self, message: dict[str, Any], session: Session) -> tuple[dict[str, Any], bool]:
        """Handle one decoded message. Returns ``(response, close_connection)``."""
        raw_id = message.get("request_id")
        fallback_id = raw_id if isinstance(raw_id, str) else None
        try:
            request_id, operation = validate_envelope(message)
        except FrameError as exc:
            self._logger.info(
                "session %s rejected request: %s", session.session_id, exc.code
            )
            return error_response(fallback_id, exc.code, exc.message), exc.fatal

        if not session.hello_done and operation != "hello":
            return (
                error_response(
                    request_id, HELLO_REQUIRED, "send a hello handshake before other operations"
                ),
                False,
            )

        if not session.remember_request(request_id):
            return (
                error_response(
                    request_id, INVALID_REQUEST, f"duplicate request_id {request_id!r}"
                ),
                False,
            )

        if operation == "hello":
            return self._handle_hello(message, session, request_id)
        if operation == "ping":
            return (
                ok_response(
                    request_id,
                    {
                        "pong": True,
                        "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "session_id": session.session_id,
                        "in_transaction": session.owns_transaction,
                    },
                ),
                False,
            )
        return self._handle_execute(message, session, request_id)

    def _handle_hello(
        self, message: dict[str, Any], session: Session, request_id: str
    ) -> tuple[dict[str, Any], bool]:
        session.protocol_version = PROTOCOL_VERSION
        session.hello_done = True
        client = message.get("client")
        if isinstance(client, dict):
            session.client_name = _sanitize(client.get("name"))
            session.client_version = _sanitize(client.get("version"))

        auth_required = bool(self.config.auth_token)
        if auth_required:
            supplied = message.get("token")
            authenticated = isinstance(supplied, str) and hmac.compare_digest(
                supplied.encode("utf-8"), self.config.auth_token.encode("utf-8")
            )
            if not authenticated:
                session.auth_failures += 1
                self._logger.warning(
                    "authentication failed session=%s peer=%s attempts=%d/%d",
                    session.session_id,
                    _peer_label(session.peer),
                    session.auth_failures,
                    self.config.auth_max_attempts,
                )
                close = session.auth_failures >= self.config.auth_max_attempts
                if close:
                    self._logger.warning(
                        "closing session %s after repeated authentication failures",
                        session.session_id,
                    )
                return (
                    error_response(request_id, AUTHENTICATION_FAILED, "authentication failed"),
                    close,
                )
            session.authenticated = True
            self._logger.info(
                "authentication succeeded session=%s peer=%s",
                session.session_id,
                _peer_label(session.peer),
            )
        else:
            session.authenticated = True

        self._logger.info(
            "handshake session=%s client=%s/%s protocol=%d tls=%s",
            session.session_id,
            session.client_name,
            session.client_version,
            PROTOCOL_VERSION,
            "on" if self.config.tls_enabled else "off",
        )
        return (
            ok_response(
                request_id,
                hello_result(
                    server_version=__version__,
                    authenticated=session.authenticated,
                    session_id=session.session_id,
                    auth_required=auth_required,
                    tls=self.config.tls_enabled,
                ),
            ),
            False,
        )

    def _handle_execute(
        self, message: dict[str, Any], session: Session, request_id: str
    ) -> tuple[dict[str, Any], bool]:
        if self.config.auth_token and not session.authenticated:
            return (
                error_response(
                    request_id, AUTHENTICATION_FAILED, "authenticate before executing SQL"
                ),
                False,
            )
        try:
            sql = validate_sql_operation(message)
        except FrameError as exc:
            return error_response(request_id, exc.code, exc.message), False

        session.requests += 1
        started = time.monotonic()
        try:
            assert self._coordinator is not None
            result = self._coordinator.execute(session, sql)
        except ServerRequestError as exc:
            self._logger.info(
                "request=%s session=%s code=%s duration=%.3fms",
                request_id,
                session.session_id,
                exc.code,
                (time.monotonic() - started) * 1000.0,
            )
            return (
                error_response(request_id, exc.code, exc.message, retryable=exc.retryable),
                False,
            )
        except Exception as exc:
            code, message = self._map_engine_error(
                exc, transaction_statement=is_transaction_statement(sql)
            )
            duration = (time.monotonic() - started) * 1000.0
            if code == INTERNAL_ERROR:
                self._logger.exception(
                    "internal error request=%s session=%s duration=%.3fms",
                    request_id,
                    session.session_id,
                    duration,
                )
                return error_response(request_id, code, "internal server error"), False
            self._logger.info(
                "request=%s session=%s code=%s duration=%.3fms",
                request_id,
                session.session_id,
                code,
                duration,
            )
            return error_response(request_id, code, message), False
        finally:
            self._requests_served += 1

        payload = dict(result)
        payload.pop("status", None)
        self._logger.info(
            "request=%s session=%s operation=execute rows=%s duration=%.3fms",
            request_id,
            session.session_id,
            payload.get("row_count", payload.get("rows_affected", 0)),
            (time.monotonic() - started) * 1000.0,
        )
        return ok_response(request_id, payload), False

    def _map_engine_error(
        self, exc: Exception, *, transaction_statement: bool = False
    ) -> tuple[str, str]:
        """Map engine exceptions onto stable protocol error codes."""
        if isinstance(exc, AmbiguousCommitError):
            return (
                RECOVERY_REQUIRED,
                (
                    "commit durability is ambiguous after a WAL fsync failure; "
                    "the database requires recovery"
                ),
            )
        if isinstance(exc, ConstraintError):
            return CONSTRAINT_ERROR, str(exc)
        if isinstance(exc, (LexerError, ParserError)):
            return SQL_PARSE_ERROR, str(exc)
        if isinstance(exc, ExecutionError):
            if transaction_statement:
                return TRANSACTION_ERROR, str(exc)
            return SQL_EXECUTION_ERROR, str(exc)
        if isinstance(exc, StorageError):
            if self._engine is not None and (
                self._engine.needs_recovery or self._engine.wal_poisoned
            ):
                return RECOVERY_REQUIRED, "database requires recovery; reopen the server"
            return DATABASE_UNAVAILABLE, str(exc)
        return INTERNAL_ERROR, "internal server error"


# --- CLI ------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aaysql.server",
        description=(
            "Run the AayushSQL database server. Binding to a non-loopback host "
            f"requires {AUTH_TOKEN_ENV} and TLS."
        ),
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="database file path")
    parser.add_argument(
        "--memory", action="store_true", help="run against an in-memory database"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default 8765)")
    parser.add_argument(
        "--max-clients", type=int, default=32, help="maximum simultaneous connections"
    )
    parser.add_argument(
        "--max-frame-size", type=int, default=DEFAULT_MAX_FRAME_SIZE, help="maximum frame bytes"
    )
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    parser.add_argument("--read-timeout", type=float, default=60.0)
    parser.add_argument("--write-timeout", type=float, default=30.0)
    parser.add_argument("--shutdown-timeout", type=float, default=10.0)
    parser.add_argument("--tls-cert", default=None, help="TLS certificate chain (PEM)")
    parser.add_argument("--tls-key", default=None, help="TLS private key (PEM)")
    parser.add_argument(
        "--tls-client-ca", default=None, help="require and verify client certificates against this CA"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="server log level",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = ServerConfig.from_environment(
            db_path=":memory:" if args.memory else args.db,
            host=args.host,
            port=args.port,
            max_clients=args.max_clients,
            max_frame_size=args.max_frame_size,
            idle_timeout=args.idle_timeout,
            read_timeout=args.read_timeout,
            write_timeout=args.write_timeout,
            shutdown_timeout=args.shutdown_timeout,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            tls_client_ca=args.tls_client_ca,
        )
        server = AayushSQLServer(config)
        host, port = server.start()
    except (ServerConfigError, StorageError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"AayushSQL v{__version__} server listening on {host}:{port} "
        f"(protocol {PROTOCOL_VERSION}); press Ctrl+C to stop",
        file=sys.stderr,
    )
    stop = threading.Event()

    def _request_stop(signum: int, frame: Any) -> None:
        LOGGER.info("received signal %s", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass

    try:
        while not stop.wait(0.2):
            if not server.started:
                break
    except KeyboardInterrupt:
        pass
    clean = server.shutdown()
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
