"""Python client for the AayushSQL server.

The public surface is intentionally small::

    from aaysql.client import AayushSQLClient

    with AayushSQLClient("127.0.0.1", 8765) as db:
        print(db.execute("AAYUSH SELECT * FROM users")["rows"])

Only :mod:`aaysql.protocol` is imported at module load, so using the client does
not pull in the engine, planner, or storage code.

Reliability contract
--------------------
The client never retries a request automatically. If the connection breaks or a
read times out after a request was sent, the server may already have committed
the mutation, so :class:`ConnectionLostError` / :class:`RequestTimeoutError`
report ``outcome_unknown = True`` and the caller must reconnect and inspect the
database instead of replaying the statement.
"""

from __future__ import annotations

import os
import socket
import ssl
import threading
import uuid
from types import TracebackType
from typing import Any

from . import __version__
from .protocol import (
    AUTH_TOKEN_ENV,
    AUTHENTICATION_FAILED,
    DEFAULT_HOST,
    DEFAULT_MAX_FRAME_SIZE,
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    REQUEST_TOO_LARGE,
    UNSUPPORTED_PROTOCOL_VERSION,
    FrameError,
    TruncatedFrame,
    decode_message,
    recv_frame,
    send_frame,
    validate_request_id,
)

__all__ = [
    "AUTH_TOKEN_ENV",
    "AayushSQLClient",
    "AayushSQLClientError",
    "AuthenticationError",
    "ConnectionError",
    "ConnectionLostError",
    "ProtocolError",
    "RequestTimeoutError",
    "ServerError",
    "TLSHandshakeError",
]

# --- exceptions -----------------------------------------------------------


class AayushSQLClientError(Exception):
    """Base class for every client-side failure."""


class ConnectionError(AayushSQLClientError):
    """The TCP connection could not be established or was closed by the server."""


class TLSHandshakeError(ConnectionError):
    """TLS negotiation or certificate verification failed."""


class AuthenticationError(ConnectionError):
    """The server rejected the configured authentication token."""


class ProtocolError(AayushSQLClientError):
    """The peer sent something that is not valid protocol v1."""


class ServerError(AayushSQLClientError):
    """The server returned a structured error response."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class RequestTimeoutError(AayushSQLClientError):
    """No response arrived within ``read_timeout``.

    ``outcome_unknown`` is always true: the request may have been executed.
    """

    outcome_unknown = True


class ConnectionLostError(AayushSQLClientError):
    """The connection failed while a request was outstanding."""

    def __init__(self, message: str, *, outcome_unknown: bool = True) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


# --- client ---------------------------------------------------------------


class AayushSQLClient:
    """A single-connection AayushSQL client.

    One instance owns one TCP connection. Requests are serialized by an internal
    lock, so a client is safe to share between threads but never pipelines.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        token: str | None = None,
        tls: bool = False,
        ca_certs: str | None = None,
        server_hostname: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        max_frame_size: int = DEFAULT_MAX_FRAME_SIZE,
        client_name: str = "aaysql-python",
    ) -> None:
        self.host = host
        self.port = port
        self.token = token if token is not None else os.environ.get(AUTH_TOKEN_ENV)
        self.tls = tls
        self.ca_certs = ca_certs
        self.server_hostname = server_hostname or host
        self.client_cert = client_cert
        self.client_key = client_key
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_frame_size = max_frame_size
        self.client_name = client_name

        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._server_version: str | None = None
        self._capabilities: tuple[str, ...] = ()
        self._authenticated = False

    # -- state ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._socket is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def server_version(self) -> str | None:
        return self._server_version

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self._capabilities

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    # -- lifecycle -----------------------------------------------------

    def __enter__(self) -> AayushSQLClient:
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> AayushSQLClient:
        """Open the socket, negotiate TLS if configured, and handshake."""
        with self._lock:
            if self._socket is not None:
                return self
            self._open_socket()
            try:
                self._handshake()
            except BaseException:
                self._abort()
                raise
            return self

    def _open_socket(self) -> None:
        try:
            raw = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            raise ConnectionError(
                f"could not connect to {self.host}:{self.port}: {exc}"
            ) from exc
        raw.settimeout(self.read_timeout)
        if not self.tls:
            self._socket = raw
            return
        try:
            context = ssl.create_default_context(cafile=self.ca_certs)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            if self.client_cert:
                context.load_cert_chain(self.client_cert, self.client_key)
            self._socket = context.wrap_socket(raw, server_hostname=self.server_hostname)
        except ssl.SSLError as exc:
            raw.close()
            self._socket = None
            raise TLSHandshakeError(f"TLS handshake failed: {exc}") from exc
        except OSError as exc:
            raw.close()
            self._socket = None
            raise TLSHandshakeError(f"TLS handshake failed: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
            self._authenticated = False
            self._session_id = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _abort(self) -> None:
        """Drop a socket whose byte stream can no longer be trusted."""
        sock, self._socket = self._socket, None
        self._authenticated = False
        self._session_id = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- operations ----------------------------------------------------

    def execute(self, sql: str) -> dict[str, Any]:
        """Execute one SQL statement and return the engine's result dictionary.

        The returned mapping matches ``Engine.execute`` (``columns``, ``rows``,
        ``row_count``, and ``message``/``rows_affected`` when applicable).
        """
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must be a non-empty string")
        return self._request("execute", sql=sql)

    def ping(self) -> dict[str, Any]:
        """Round-trip a ``ping`` and return the server's result mapping."""
        return self._request("ping")

    # -- protocol ------------------------------------------------------

    def _handshake(self) -> None:
        request_id = uuid.uuid4().hex
        message = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": "hello",
            "client": {"name": self.client_name, "version": __version__},
        }
        if self.token is not None:
            message["token"] = self.token
        response = self._exchange(message, request_id)
        result = response.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("handshake response is missing a result object")
        version = result.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"server reported protocol version {version!r}, client speaks {PROTOCOL_VERSION}"
            )
        self._session_id = result.get("session_id")
        self._server_version = result.get("server_version")
        capabilities = result.get("capabilities")
        self._capabilities = tuple(capabilities) if isinstance(capabilities, list) else ()
        self._authenticated = bool(result.get("authenticated"))

    def _request(self, operation: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            if self._socket is None:
                raise ConnectionError("client is not connected; call connect() first")
            request_id = uuid.uuid4().hex
            message = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "operation": operation,
                **fields,
            }
            response = self._exchange(message, request_id)
            result = response.get("result")
            if not isinstance(result, dict):
                raise ProtocolError("response is missing a result object")
            return result

    def _exchange(self, message: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Send one request and read its response. Must be called under the lock."""
        sock = self._socket
        if sock is None:
            raise ConnectionError("client is not connected")

        try:
            send_frame(
                sock,
                message,
                self.max_frame_size,
                too_large_code=REQUEST_TOO_LARGE,
            )
        except TimeoutError as exc:
            self._abort()
            raise RequestTimeoutError(
                f"timed out sending request {request_id}"
            ) from exc
        except FrameError as exc:
            self._abort()
            raise ProtocolError(f"{exc.code}: {exc.message}") from exc
        except OSError as exc:
            self._abort()
            raise ConnectionLostError(
                f"connection lost while sending request {request_id}: {exc}"
            ) from exc

        try:
            payload = recv_frame(sock, self.max_frame_size)
        except TimeoutError as exc:
            self._abort()
            raise RequestTimeoutError(
                f"timed out waiting for a response to request {request_id}"
            ) from exc
        except TruncatedFrame as exc:
            self._abort()
            raise ConnectionLostError(
                f"connection closed mid-response for request {request_id}: {exc}"
            ) from exc
        except FrameError as exc:
            self._abort()
            raise ProtocolError(f"{exc.code}: {exc.message}") from exc
        except OSError as exc:
            self._abort()
            raise ConnectionLostError(
                f"connection lost while reading the response to request {request_id}: {exc}"
            ) from exc

        if payload is None:
            self._abort()
            raise ConnectionLostError(
                f"server closed the connection without answering request {request_id}"
            )
        try:
            response = decode_message(payload)
        except FrameError as exc:
            self._abort()
            raise ProtocolError(f"{exc.code}: {exc.message}") from exc
        return self._validate_response(response, request_id)

    def _validate_response(self, response: dict[str, Any], request_id: str) -> dict[str, Any]:
        version = response.get("protocol_version")
        if version != PROTOCOL_VERSION:
            self._abort()
            raise ProtocolError(
                f"response protocol_version {version!r} does not match {PROTOCOL_VERSION}"
            )
        status = response.get("status")
        raw_id = response.get("request_id")
        if raw_id is None:
            # A server may reject a connection before it has a request to
            # correlate (connection limit, oversized frame). That is only
            # acceptable for an error envelope.
            if status != "error":
                self._abort()
                raise ProtocolError("response is missing a request_id")
        else:
            try:
                returned_id = validate_request_id(raw_id)
            except FrameError as exc:
                self._abort()
                raise ProtocolError(f"malformed response: {exc.message}") from exc
            if returned_id != request_id:
                self._abort()
                raise ProtocolError(
                    f"response request_id {returned_id!r} does not match request {request_id!r}"
                )

        if status == "ok":
            return response
        if status != "error":
            self._abort()
            raise ProtocolError(f"unknown response status {status!r}")

        error = response.get("error")
        if not isinstance(error, dict):
            raise ProtocolError("error response is missing an 'error' object")
        code = error.get("code")
        message = error.get("message")
        retryable = error.get("retryable", False)
        if not isinstance(code, str) or not isinstance(message, str):
            raise ProtocolError("error response has an invalid code or message")
        if code == AUTHENTICATION_FAILED:
            raise AuthenticationError(message)
        if code == UNSUPPORTED_PROTOCOL_VERSION:
            raise ProtocolError(message)
        raise ServerError(code, message, bool(retryable))
