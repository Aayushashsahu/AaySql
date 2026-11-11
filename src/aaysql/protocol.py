"""AayushSQL network protocol v1: framing, envelopes, and validation.

Framing is deliberately boring and explicit::

    +----------------+---------------------------+
    | 4-byte uint32  |  UTF-8 JSON payload       |
    |  big-endian    |  (<= max_frame_size)      |
    |  payload size  |                           |
    +----------------+---------------------------+

A receiver reads the header, rejects lengths it cannot afford to buffer *before*
allocating anything, then reads exactly that many payload bytes. There are no
newline delimiters and no bare SQL strings: every request and response is a JSON
object inside one frame.

The network protocol version (``PROTOCOL_VERSION``) is independent of the
package/engine version. It only changes when the wire format changes
incompatibly; adding an operation or a result field does not require a bump.
"""

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL_VERSION = 1

#: Environment variable holding the optional bearer token for the server.
AUTH_TOKEN_ENV = "AAYUSQL_AUTH_TOKEN"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_FRAME_SIZE = 1024 * 1024
HEADER_SIZE = 4
MAX_REQUEST_ID_LENGTH = 128

SUPPORTED_OPERATIONS = frozenset({"hello", "ping", "execute"})
CAPABILITIES = ("execute", "transactions", "ping")

# --- stable error codes ---------------------------------------------------

INVALID_REQUEST = "INVALID_REQUEST"
UNSUPPORTED_PROTOCOL_VERSION = "UNSUPPORTED_PROTOCOL_VERSION"
AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
SQL_PARSE_ERROR = "SQL_PARSE_ERROR"
SQL_EXECUTION_ERROR = "SQL_EXECUTION_ERROR"
CONSTRAINT_ERROR = "CONSTRAINT_ERROR"
TRANSACTION_BUSY = "TRANSACTION_BUSY"
TRANSACTION_ERROR = "TRANSACTION_ERROR"
REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
CONNECTION_TIMEOUT = "CONNECTION_TIMEOUT"
TOO_MANY_CONNECTIONS = "TOO_MANY_CONNECTIONS"
HELLO_REQUIRED = "HELLO_REQUIRED"
UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
INTERNAL_ERROR = "INTERNAL_ERROR"

ERROR_CODES = frozenset(
    {
        INVALID_REQUEST,
        UNSUPPORTED_PROTOCOL_VERSION,
        AUTHENTICATION_FAILED,
        SQL_PARSE_ERROR,
        SQL_EXECUTION_ERROR,
        CONSTRAINT_ERROR,
        TRANSACTION_BUSY,
        TRANSACTION_ERROR,
        REQUEST_TOO_LARGE,
        RESPONSE_TOO_LARGE,
        DATABASE_UNAVAILABLE,
        RECOVERY_REQUIRED,
        CONNECTION_TIMEOUT,
        TOO_MANY_CONNECTIONS,
        HELLO_REQUIRED,
        UNKNOWN_OPERATION,
        INTERNAL_ERROR,
    }
)

# Errors a client may safely retry *without* the request having taken effect.
RETRYABLE_CODES = frozenset({TRANSACTION_BUSY, TOO_MANY_CONNECTIONS, CONNECTION_TIMEOUT})


class FrameError(Exception):
    """A framing or envelope error that maps to a protocol error code."""

    def __init__(self, code: str, message: str, *, fatal: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # ``fatal`` means the byte stream is no longer trustworthy (truncated
        # frame, oversized length, empty frame) and the connection must close.
        self.fatal = fatal


class TruncatedFrame(Exception):
    """The peer closed the connection in the middle of a frame."""


# --- framing --------------------------------------------------------------


def encode_message(message: dict[str, Any]) -> bytes:
    """Serialize one envelope to UTF-8 JSON bytes."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def frame_bytes(payload: bytes) -> bytes:
    """Prefix a payload with its 4-byte big-endian length."""
    return struct.pack("!I", len(payload)) + payload


def _recv_exact(sock: Any, count: int) -> bytes:
    """Read exactly ``count`` bytes, tolerating partial reads.

    Raises :class:`TruncatedFrame` if the peer closes before the frame is complete.
    A ``socket.timeout`` from the socket propagates to the caller.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise TruncatedFrame(f"connection closed with {remaining} byte(s) outstanding")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: Any, max_frame_size: int) -> bytes | None:
    """Read one frame. Returns ``None`` on a clean close between frames.

    The length is validated against ``max_frame_size`` *before* any payload is
    buffered, so an untrusted header cannot trigger a huge allocation.
    """
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(header))
        if not chunk:
            if not header:
                return None
            raise TruncatedFrame("connection closed inside a frame header")
        header += chunk

    (length,) = struct.unpack("!I", header)
    if length == 0:
        raise FrameError(INVALID_REQUEST, "frame length must be greater than zero")
    if length > max_frame_size:
        raise FrameError(
            REQUEST_TOO_LARGE,
            f"frame length {length} exceeds the maximum of {max_frame_size} bytes",
        )
    return _recv_exact(sock, length)


class FrameReader:
    """Incremental frame assembler for a stream socket.

    Bytes may arrive in arbitrary chunks and several frames may arrive in one
    read; :meth:`pop` returns exactly one complete payload at a time. Unlike
    :func:`recv_frame`, partial state survives between reads, so a caller can
    distinguish an idle connection from a half-received frame.
    """

    def __init__(self, max_frame_size: int) -> None:
        self.max_frame_size = max_frame_size
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    @property
    def has_partial(self) -> bool:
        return bool(self._buffer)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def pop(self) -> bytes | None:
        """Return the next complete payload, or ``None`` if more bytes are needed."""
        if self._expected is None:
            if len(self._buffer) < HEADER_SIZE:
                return None
            (length,) = struct.unpack("!I", bytes(self._buffer[:HEADER_SIZE]))
            if length == 0:
                raise FrameError(INVALID_REQUEST, "frame length must be greater than zero")
            if length > self.max_frame_size:
                raise FrameError(
                    REQUEST_TOO_LARGE,
                    f"frame length {length} exceeds the maximum of "
                    f"{self.max_frame_size} bytes",
                )
            self._expected = length
        total = HEADER_SIZE + self._expected
        if len(self._buffer) < total:
            return None
        payload = bytes(self._buffer[HEADER_SIZE:total])
        del self._buffer[:total]
        self._expected = None
        return payload


def decode_message(payload: bytes) -> dict[str, Any]:
    """Decode a frame payload into a JSON object envelope."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrameError(INVALID_REQUEST, "payload is not valid UTF-8") from exc
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameError(INVALID_REQUEST, "payload is not valid JSON") from exc
    if not isinstance(message, dict):
        raise FrameError(INVALID_REQUEST, "payload must be a JSON object")
    return message


def send_frame(sock: Any, message: dict[str, Any], max_frame_size: int) -> int:
    """Encode and send one envelope. Returns the payload length."""
    payload = encode_message(message)
    if len(payload) > max_frame_size:
        raise FrameError(
            RESPONSE_TOO_LARGE,
            f"response of {len(payload)} bytes exceeds the maximum of {max_frame_size} bytes",
        )
    sock.sendall(frame_bytes(payload))
    return len(payload)


# --- envelopes ------------------------------------------------------------


def ok_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "ok",
        "result": result,
    }


def error_response(
    request_id: Any,
    code: str,
    message: str,
    *,
    retryable: bool | None = None,
) -> dict[str, Any]:
    if retryable is None:
        retryable = code in RETRYABLE_CODES
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
        },
    }


def validate_request_id(value: Any) -> str:
    """Validate a client-supplied correlation id (never an authorization token)."""
    if not isinstance(value, str):
        raise FrameError(INVALID_REQUEST, "request_id must be a string", fatal=False)
    if not value:
        raise FrameError(INVALID_REQUEST, "request_id must not be empty", fatal=False)
    if len(value) > MAX_REQUEST_ID_LENGTH:
        raise FrameError(
            INVALID_REQUEST,
            f"request_id must be at most {MAX_REQUEST_ID_LENGTH} characters",
            fatal=False,
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise FrameError(
            INVALID_REQUEST,
            "request_id must not contain control characters",
            fatal=False,
        )
    return value


def validate_envelope(message: dict[str, Any]) -> tuple[str, str]:
    """Validate protocol version and operation. Returns ``(request_id, operation)``.

    Envelope-level problems are non-fatal at the framing layer: the stream is
    still aligned, so the server answers with a structured error and keeps the
    connection open.
    """
    request_id = message.get("request_id")
    version = message.get("protocol_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise FrameError(
            INVALID_REQUEST,
            f"protocol_version must be the integer {PROTOCOL_VERSION}",
            fatal=False,
        )
    if version != PROTOCOL_VERSION:
        raise FrameError(
            UNSUPPORTED_PROTOCOL_VERSION,
            f"unsupported protocol_version {version}; this server speaks version {PROTOCOL_VERSION}",
            fatal=False,
        )
    # Validate the id early so every response can be correlated.
    validate_request_id(request_id)
    operation = message.get("operation")
    if not isinstance(operation, str) or not operation:
        raise FrameError(INVALID_REQUEST, "operation must be a non-empty string", fatal=False)
    if operation not in SUPPORTED_OPERATIONS:
        raise FrameError(UNKNOWN_OPERATION, f"unknown operation {operation!r}", fatal=False)
    return request_id, operation


def validate_sql_operation(message: dict[str, Any]) -> str:
    sql = message.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise FrameError(
            INVALID_REQUEST,
            "execute requires a non-empty 'sql' string",
            fatal=False,
        )
    return sql


def hello_result(
    *,
    server_version: str,
    authenticated: bool,
    session_id: str,
    auth_required: bool,
    tls: bool,
) -> dict[str, Any]:
    return {
        "server_version": server_version,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": list(CAPABILITIES),
        "authenticated": authenticated,
        "auth_required": auth_required,
        "session_id": session_id,
        "tls": tls,
    }
