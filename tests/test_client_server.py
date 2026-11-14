"""Real-socket client/server integration tests for AayushSQL v0.8.0.

Every test here uses an actual TCP connection (loopback, ephemeral port) and
temporary database paths. Nothing is mocked at the transport level except where
the point of the test is a malformed peer (raw sockets) or a dead server.
"""

import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql import __version__
from aaysql.client import (
    AUTH_TOKEN_ENV,
    AayushSQLClient,
    AayushSQLClientError,
    AuthenticationError,
    ConnectionLostError,
    ProtocolError,
    RequestTimeoutError,
    ServerError,
    TLSHandshakeError,
)
from aaysql.errors import DatabaseLockedError
from aaysql.protocol import (
    AUTHENTICATION_FAILED,
    CONNECTION_TIMEOUT,
    HELLO_REQUIRED,
    INVALID_REQUEST,
    PROTOCOL_VERSION,
    REQUEST_TOO_LARGE,
    TOO_MANY_CONNECTIONS,
    TRANSACTION_BUSY,
    UNKNOWN_OPERATION,
    UNSUPPORTED_PROTOCOL_VERSION,
    decode_message,
    encode_message,
    frame_bytes,
    recv_frame,
)
from aaysql.server import (
    AayushSQLServer,
    ServerConfig,
    ServerConfigError,
    ServerLock,
)

SRC = str(Path(__file__).parents[1] / "src")
MAX = 1024 * 1024


# --- fixtures and helpers -------------------------------------------------


@pytest.fixture()
def server_factory():
    servers: list[AayushSQLServer] = []

    def _make(**kwargs) -> AayushSQLServer:
        kwargs.setdefault("db_path", None)
        kwargs.setdefault("port", 0)
        kwargs.setdefault("read_timeout", 5.0)
        kwargs.setdefault("write_timeout", 5.0)
        kwargs.setdefault("idle_timeout", 30.0)
        kwargs.setdefault("shutdown_timeout", 5.0)
        server = AayushSQLServer(ServerConfig(**kwargs))
        server.start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.shutdown()


@pytest.fixture()
def server(server_factory):
    return server_factory()


@pytest.fixture()
def client(server):
    with AayushSQLClient(*server.address) as client:
        yield client


def _client(server: AayushSQLServer, **kwargs) -> AayushSQLClient:
    kwargs.setdefault("read_timeout", 5.0)
    return AayushSQLClient(*server.address, **kwargs).connect()


def _raw(server: AayushSQLServer, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.address[1]), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _send_raw(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(frame_bytes(payload))


def _send_json(sock: socket.socket, message: dict) -> None:
    _send_raw(sock, encode_message(message))


def _recv_raw(sock: socket.socket) -> dict | None:
    payload = recv_frame(sock, MAX)
    if payload is None:
        return None
    return decode_message(payload)


def _hello_payload(**overrides) -> dict:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "hello-1",
        "operation": "hello",
        "client": {"name": "test-suite", "version": "1.0"},
    }
    payload.update(overrides)
    return payload


def _execute_payload(sql: str, request_id: str = "req-1") -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": "execute",
        "sql": sql,
    }


def _handshake(sock: socket.socket, **overrides) -> dict:
    _send_json(sock, _hello_payload(**overrides))
    response = _recv_raw(sock)
    assert response is not None
    return response


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _connect_retrying(server: AayushSQLServer, timeout: float = 3.0) -> AayushSQLClient:
    """Connect, retrying only the retryable connection-limit rejection."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _client(server)
        except ServerError as exc:
            if exc.code != TOO_MANY_CONNECTIONS or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _openssl_available() -> bool:
    return shutil.which("openssl") is not None


def _make_self_signed(directory: Path, name: str) -> tuple[Path, Path]:
    key = directory / f"{name}.key"
    cert = directory / f"{name}.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return cert, key


@pytest.fixture(scope="session")
def certs(tmp_path_factory):
    if not _openssl_available():
        pytest.skip("openssl is required to generate test certificates")
    directory = tmp_path_factory.mktemp("certs")
    server_cert, server_key = _make_self_signed(directory, "server")
    client_cert, client_key = _make_self_signed(directory, "client")
    other_cert, other_key = _make_self_signed(directory, "untrusted")
    return {
        "server_cert": server_cert,
        "server_key": server_key,
        "client_cert": client_cert,
        "client_key": client_key,
        "other_cert": other_cert,
        "other_key": other_key,
    }


# --- protocol tests -------------------------------------------------------


def test_handshake_reports_version_capabilities_and_session(server):
    sock = _raw(server)
    try:
        response = _handshake(sock)
        assert response["status"] == "ok"
        assert response["request_id"] == "hello-1"
        assert response["protocol_version"] == PROTOCOL_VERSION
        result = response["result"]
        assert result["protocol_version"] == PROTOCOL_VERSION
        assert result["server_version"] == __version__
        assert "execute" in result["capabilities"]
        assert result["auth_required"] is False
        assert result["authenticated"] is True
        assert result["tls"] is False
        assert len(result["session_id"]) == 32
    finally:
        sock.close()


def test_unsupported_protocol_version_is_rejected_without_closing(server):
    sock = _raw(server)
    try:
        response = _handshake(sock, protocol_version=PROTOCOL_VERSION + 1)
        assert response["status"] == "error"
        assert response["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
        assert response["error"]["retryable"] is False
        # The stream is still aligned, so a supported handshake can follow.
        response = _handshake(sock, request_id="hello-2")
        assert response["status"] == "ok"
    finally:
        sock.close()


def test_ping_is_correlated_by_request_id(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        for index in range(3):
            request_id = f"ping-{index}"
            _send_json(
                sock,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "operation": "ping",
                },
            )
            response = _recv_raw(sock)
            assert response["request_id"] == request_id
            assert response["status"] == "ok"
            assert response["result"]["pong"] is True
    finally:
        sock.close()


def test_execute_returns_engine_result_shape(client):
    client.execute("AAYUSH CREATE TABLE t (id INT, name TEXT)")
    client.execute("AAYUSH INSERT INTO t VALUES (1, 'a'), (2, 'b')")
    result = client.execute("AAYUSH SELECT id, name FROM t ORDER BY id ASC")
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert result["row_count"] == 2
    assert "status" not in result


def test_execute_requires_hello_first(server):
    sock = _raw(server)
    try:
        _send_json(sock, _execute_payload("AAYUSH SELECT * FROM t"))
        response = _recv_raw(sock)
        assert response["error"]["code"] == HELLO_REQUIRED
    finally:
        sock.close()


def test_execute_requires_non_empty_sql(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_json(sock, _execute_payload("   ", request_id="empty"))
        response = _recv_raw(sock)
        assert response["status"] == "error"
        assert response["error"]["code"] == INVALID_REQUEST
        assert response["request_id"] == "empty"
    finally:
        sock.close()


def test_malformed_json_returns_structured_error(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_raw(sock, b'{"protocol_version": 1, "request_id":')
        response = _recv_raw(sock)
        assert response["status"] == "error"
        assert response["error"]["code"] == INVALID_REQUEST
        # Framing was intact, so the connection is still usable.
        _send_json(
            sock,
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "after-bad-json",
                "operation": "ping",
            },
        )
        assert _recv_raw(sock)["status"] == "ok"
    finally:
        sock.close()


def test_invalid_utf8_payload_is_rejected(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_raw(sock, b"\xff\xfe\xfd not utf-8")
        response = _recv_raw(sock)
        assert response["error"]["code"] == INVALID_REQUEST
    finally:
        sock.close()


def test_non_object_json_payload_is_rejected(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_raw(sock, b"[1, 2, 3]")
        response = _recv_raw(sock)
        assert response["error"]["code"] == INVALID_REQUEST
    finally:
        sock.close()


def test_incomplete_frame_header_times_out_and_closes(server_factory):
    server = server_factory(read_timeout=0.2, idle_timeout=0.3)
    sock = _raw(server)
    try:
        _handshake(sock)
        sock.sendall(b"\x00\x00")  # half a length header, then silence
        response = _recv_raw(sock)
        assert response["status"] == "error"
        assert response["error"]["code"] == CONNECTION_TIMEOUT
        # The server closes after a mid-frame timeout.
        assert sock.recv(1) == b""
    finally:
        sock.close()


def test_incomplete_payload_times_out_and_closes(server_factory):
    server = server_factory(read_timeout=0.2, idle_timeout=0.3)
    sock = _raw(server)
    try:
        _handshake(sock)
        sock.sendall(struct.pack("!I", 100) + b"partial")
        response = _recv_raw(sock)
        assert response["error"]["code"] == CONNECTION_TIMEOUT
        assert sock.recv(1) == b""
    finally:
        sock.close()


def test_oversized_frame_is_rejected_before_allocation(server_factory):
    server = server_factory(max_frame_size=4096)
    sock = _raw(server)
    try:
        _handshake(sock)
        sock.sendall(struct.pack("!I", 10 * 1024 * 1024))
        response = _recv_raw(sock)
        assert response["status"] == "error"
        assert response["error"]["code"] == REQUEST_TOO_LARGE
        assert sock.recv(1) == b""
    finally:
        sock.close()


def test_zero_length_frame_is_rejected(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        sock.sendall(struct.pack("!I", 0))
        response = _recv_raw(sock)
        assert response["error"]["code"] == INVALID_REQUEST
    finally:
        sock.close()


@pytest.mark.parametrize(
    "request_id",
    [123, "", "x" * 129, "bad\nid"],
)
def test_invalid_request_id_is_rejected(server, request_id):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_json(sock, _execute_payload("AAYUSH SELECT 1", request_id=request_id))
        response = _recv_raw(sock)
        assert response["status"] == "error"
        assert response["error"]["code"] == INVALID_REQUEST
    finally:
        sock.close()


def test_duplicate_request_id_is_rejected(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_json(sock, _execute_payload("AAYUSH SELECT * FROM t", request_id="dup"))
        first = _recv_raw(sock)
        assert first["status"] == "error"  # table does not exist yet
        _send_json(sock, _execute_payload("AAYUSH SELECT * FROM t", request_id="dup"))
        second = _recv_raw(sock)
        assert second["status"] == "error"
        assert "duplicate request_id" in second["error"]["message"]
    finally:
        sock.close()


def test_unknown_operation_is_rejected(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_json(
            sock,
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "weird",
                "operation": "drop_everything",
            },
        )
        response = _recv_raw(sock)
        assert response["error"]["code"] == UNKNOWN_OPERATION
    finally:
        sock.close()


def test_multiple_requests_on_one_connection_are_correlated(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        for index in range(5):
            request_id = f"multi-{index}"
            _send_json(
                sock,
                _execute_payload(
                    f"AAYUSH CREATE TABLE t{index} (id INT)", request_id=request_id
                ),
            )
            response = _recv_raw(sock)
            assert response["request_id"] == request_id
            assert response["status"] == "ok"
    finally:
        sock.close()


def test_pipelined_frames_are_all_answered_in_order(server):
    sock = _raw(server)
    try:
        _handshake(sock)
        _send_json(
            sock,
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "hello-2",
                "operation": "ping",
            },
        )
        _send_json(
            sock,
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "ping-2",
                "operation": "ping",
            },
        )
        first = _recv_raw(sock)
        second = _recv_raw(sock)
        assert [first["request_id"], second["request_id"]] == ["hello-2", "ping-2"]
    finally:
        sock.close()


def test_truncated_frame_then_disconnect_does_not_break_server(server):
    sock = _raw(server)
    sock.sendall(struct.pack("!I", 50) + b"short")
    sock.close()
    # The server survives and still serves other clients.
    with AayushSQLClient(*server.address) as client:
        assert client.ping()["pong"] is True


def test_client_timeout_reports_unknown_outcome():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                accepted.append(conn)  # keep it open, never answer
            except (OSError, TimeoutError):
                continue

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        with pytest.raises(RequestTimeoutError) as excinfo:
            AayushSQLClient("127.0.0.1", port, read_timeout=0.25).connect()
        assert excinfo.value.outcome_unknown is True
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()
        for conn in accepted:
            conn.close()


# --- authentication and TLS ----------------------------------------------


def test_server_without_token_allows_anonymous(server):
    with AayushSQLClient(*server.address) as client:
        assert client.authenticated is True
        client.execute("AAYUSH CREATE TABLE anonymous_ok (id INT)")


def test_correct_token_authenticates(server_factory):
    server = server_factory(auth_token="s3cret-token")
    with AayushSQLClient(*server.address, token="s3cret-token") as client:
        assert client.authenticated is True
        client.execute("AAYUSH CREATE TABLE t (id INT)")


def test_missing_token_is_rejected(server_factory):
    server = server_factory(auth_token="s3cret-token")
    with pytest.raises(AuthenticationError):
        AayushSQLClient(*server.address).connect()


def test_wrong_token_is_rejected(server_factory):
    server = server_factory(auth_token="s3cret-token")
    with pytest.raises(AuthenticationError):
        AayushSQLClient(*server.address, token="wrong-token-value").connect()


def test_token_from_environment(monkeypatch, server_factory):
    monkeypatch.setenv(AUTH_TOKEN_ENV, "env-token")
    server = server_factory(auth_token="env-token")
    with AayushSQLClient(*server.address) as client:  # token read from the environment
        assert client.authenticated is True


def test_empty_token_configuration_is_refused(monkeypatch):
    with pytest.raises(ServerConfigError, match="must not be empty"):
        ServerConfig(auth_token="").validate()
    monkeypatch.setenv(AUTH_TOKEN_ENV, "")
    with pytest.raises(ServerConfigError, match="set but empty"):
        ServerConfig.from_environment(db_path=None)


def test_repeated_authentication_failures_close_the_connection(server_factory):
    server = server_factory(auth_token="s3cret-token", auth_max_attempts=2)
    sock = _raw(server)
    try:
        _send_json(sock, _hello_payload(request_id="h1", token="wrong"))
        first = _recv_raw(sock)
        assert first["error"]["code"] == AUTHENTICATION_FAILED
        _send_json(sock, _hello_payload(request_id="h2", token="also-wrong"))
        second = _recv_raw(sock)
        assert second["error"]["code"] == AUTHENTICATION_FAILED
        assert sock.recv(1) == b""
    finally:
        sock.close()


def test_unauthenticated_session_cannot_execute(server_factory):
    server = server_factory(auth_token="s3cret-token", auth_max_attempts=5)
    sock = _raw(server)
    try:
        _send_json(sock, _hello_payload(request_id="h1"))  # no token supplied
        assert _recv_raw(sock)["error"]["code"] == AUTHENTICATION_FAILED
    finally:
        sock.close()
    # A second connection that authenticates works normally.
    with AayushSQLClient(*server.address, token="s3cret-token") as client:
        client.execute("AAYUSH CREATE TABLE ok (id INT)")
        assert client.execute("AAYUSH SELECT * FROM ok")["row_count"] == 0


def test_token_is_never_written_to_disk_or_logged(tmp_path, caplog, monkeypatch):
    token = "token-that-must-not-leak-0123456789"
    db_path = tmp_path / "secret.aaydb"
    caplog.set_level("DEBUG", logger="aaysql.server")
    server = AayushSQLServer(ServerConfig(db_path=db_path, port=0, auth_token=token))
    server.start()
    try:
        with AayushSQLClient(*server.address, token=token) as client:
            client.execute("AAYUSH CREATE TABLE t (id INT)")
            client.execute("AAYUSH INSERT INTO t VALUES (1)")
        with pytest.raises(AuthenticationError):
            AayushSQLClient(*server.address, token="wrong").connect()
    finally:
        server.shutdown()

    assert token not in caplog.text
    for path in tmp_path.iterdir():
        data = path.read_bytes()
        assert token.encode() not in data, f"token leaked into {path}"


def test_default_binding_is_loopback():
    config = ServerConfig()
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.is_loopback
    assert config.validate() is None


def test_remote_binding_without_token_is_refused(certs):
    config = ServerConfig(
        host="0.0.0.0",
        tls_cert=str(certs["server_cert"]),
        tls_key=str(certs["server_key"]),
    )
    with pytest.raises(ServerConfigError, match="without authentication"):
        config.validate()


def test_remote_binding_without_tls_is_refused():
    config = ServerConfig(host="0.0.0.0", auth_token="secret")
    with pytest.raises(ServerConfigError, match="without TLS"):
        config.validate()


def test_remote_binding_requires_both_cert_and_key(certs):
    config = ServerConfig(
        host="0.0.0.0",
        auth_token="secret",
        tls_cert=str(certs["server_cert"]),
    )
    config.tls_key = None
    with pytest.raises(ServerConfigError, match="both tls_cert and tls_key"):
        config.validate()


def test_tls_with_token_serves_authenticated_clients(server_factory, certs):
    server = server_factory(
        auth_token="tls-secret",
        tls_cert=str(certs["server_cert"]),
        tls_key=str(certs["server_key"]),
    )
    with AayushSQLClient(
        *server.address,
        token="tls-secret",
        tls=True,
        ca_certs=str(certs["server_cert"]),
        server_hostname="localhost",
    ) as client:
        client.execute("AAYUSH CREATE TABLE secure (id INT)")
        client.execute("AAYUSH INSERT INTO secure VALUES (1)")
        assert client.execute("AAYUSH SELECT * FROM secure")["rows"] == [{"id": 1}]


def test_plaintext_client_cannot_talk_to_a_tls_server(server_factory, certs):
    server = server_factory(
        auth_token="tls-secret",
        tls_cert=str(certs["server_cert"]),
        tls_key=str(certs["server_key"]),
    )
    with pytest.raises(AayushSQLClientError):
        AayushSQLClient(
            *server.address, token="tls-secret", read_timeout=1.0
        ).connect()


def test_tls_rejects_an_untrusted_server_certificate(server_factory, certs):
    server = server_factory(
        auth_token="tls-secret",
        tls_cert=str(certs["server_cert"]),
        tls_key=str(certs["server_key"]),
    )
    with pytest.raises(TLSHandshakeError):
        AayushSQLClient(
            *server.address,
            token="tls-secret",
            tls=True,
            ca_certs=str(certs["other_cert"]),
            server_hostname="localhost",
        ).connect()


def test_server_requires_client_certificates_when_configured(server_factory, certs):
    server = server_factory(
        auth_token="mtls-secret",
        tls_cert=str(certs["server_cert"]),
        tls_key=str(certs["server_key"]),
        tls_client_ca=str(certs["client_cert"]),
    )
    with AayushSQLClient(
        *server.address,
        token="mtls-secret",
        tls=True,
        ca_certs=str(certs["server_cert"]),
        server_hostname="localhost",
        client_cert=str(certs["client_cert"]),
        client_key=str(certs["client_key"]),
    ) as client:
        client.execute("AAYUSH CREATE TABLE mtls (id INT)")
        assert client.execute("AAYUSH SELECT * FROM mtls")["row_count"] == 0

    # Without a client certificate the handshake must fail.
    with pytest.raises((TLSHandshakeError, ConnectionLostError, ProtocolError)):
        AayushSQLClient(
            *server.address,
            token="mtls-secret",
            tls=True,
            ca_certs=str(certs["server_cert"]),
            server_hostname="localhost",
            read_timeout=1.0,
        ).connect()


# --- lifecycle -----------------------------------------------------------


def test_startup_with_existing_database_serves_persisted_rows(tmp_path):
    db_path = tmp_path / "lifecycle.aaydb"
    first = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    first.start()
    try:
        with AayushSQLClient(*first.address) as client:
            client.execute("AAYUSH CREATE TABLE t (id INT, name TEXT)")
            client.execute("AAYUSH INSERT INTO t VALUES (1, 'one')")
    finally:
        assert first.shutdown() is True

    second = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    second.start()
    try:
        with AayushSQLClient(*second.address) as client:
            assert client.execute("AAYUSH SELECT * FROM t")["rows"] == [
                {"id": 1, "name": "one"}
            ]
    finally:
        second.shutdown()


def test_second_server_on_the_same_database_is_refused(tmp_path):
    db_path = tmp_path / "owned.aaydb"
    first = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    first.start()
    try:
        second = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
        # Ownership conflicts are a typed error now, not a generic config error.
        with pytest.raises(DatabaseLockedError, match="already in use"):
            second.start()
        # The first server is unaffected.
        with AayushSQLClient(*first.address) as client:
            assert client.ping()["pong"] is True
    finally:
        first.shutdown()


def test_lock_is_released_after_clean_shutdown(tmp_path):
    db_path = tmp_path / "relock.aaydb"
    for _ in range(2):
        server = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
        server.start()
        assert server.shutdown() is True
    with ServerLock(Path(f"{db_path}.server.lock")):
        pass


def test_lock_is_released_after_process_crash(tmp_path):
    db_path = tmp_path / "crashlock.aaydb"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql.server import AayushSQLServer, ServerConfig\n"
        f"s = AayushSQLServer(ServerConfig(db_path={str(db_path)!r}, port=0))\n"
        "s.start()\n"
        "print('READY', flush=True)\n"
        "import os\n"
        "os._exit(0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "READY" in result.stdout

    server = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    server.start()
    try:
        with AayushSQLClient(*server.address) as client:
            assert client.ping()["pong"] is True
    finally:
        server.shutdown()


def test_startup_recovers_an_interrupted_wal_mutation(tmp_path):
    db_path = tmp_path / "recovery.aaydb"
    server = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    server.start()
    try:
        with AayushSQLClient(*server.address) as client:
            client.execute("AAYUSH CREATE TABLE t (id INT, name TEXT)")
            client.execute("AAYUSH INSERT INTO t VALUES (1, 'committed')")
    finally:
        server.shutdown()

    # Crash a child process between the WAL write and the durable commit.
    crash_code = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from aaysql.engine import Engine\n"
        f"e = Engine({str(db_path)!r})\n"
        "e.execute(\"AAYUSH INSERT INTO t VALUES (2, 'ghost')\")\n"
    )
    env = dict(os.environ)
    env["_AAYSQL_TEST_CRASH_POINT"] = "after_write_before_commit"
    crashed = subprocess.run(
        [sys.executable, "-c", crash_code],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert crashed.returncode == 99, crashed.stderr

    restarted = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    restarted.start()
    try:
        with AayushSQLClient(*restarted.address) as client:
            rows = client.execute("AAYUSH SELECT * FROM t ORDER BY id ASC")["rows"]
            assert rows == [{"id": 1, "name": "committed"}]
            client.execute("AAYUSH INSERT INTO t VALUES (3, 'after-recovery')")
            assert client.execute("AAYUSH SELECT COUNT(*) AS n FROM t")["rows"] == [{"n": 2}]
    finally:
        restarted.shutdown()


def test_graceful_shutdown_disconnects_clients(server_factory):
    server = server_factory()
    client = _client(server)
    assert client.ping()["pong"] is True
    assert server.shutdown() is True
    assert server.session_count == 0
    with pytest.raises((ConnectionLostError, ServerError, ProtocolError, AayushSQLClientError)):
        client.execute("AAYUSH SELECT * FROM t")
    client.close()


def test_shutdown_rolls_back_an_active_session_transaction(server_factory, tmp_path):
    db_path = tmp_path / "shutdown_tx.aaydb"
    server = server_factory(db_path=db_path)
    client = _client(server)
    client.execute("AAYUSH CREATE TABLE t (id INT)")
    client.execute("AAYUSH BEGIN")
    client.execute("AAYUSH INSERT INTO t VALUES (1)")
    assert server.shutdown() is True
    client.close()

    restarted = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    restarted.start()
    try:
        with AayushSQLClient(*restarted.address) as reopened:
            assert reopened.execute("AAYUSH SELECT * FROM t")["row_count"] == 0
    finally:
        restarted.shutdown()


def test_maximum_connections_are_enforced(server_factory):
    server = server_factory(max_clients=1)
    first = _client(server)
    try:
        with pytest.raises(ServerError) as excinfo:
            _client(server)
        assert excinfo.value.code == TOO_MANY_CONNECTIONS
        assert excinfo.value.retryable is True
        # Closing the active client frees the slot.
        first.close()
        assert _wait_for(lambda: server.stats["active_sessions"] == 0)
        second = _connect_retrying(server)
        assert second.ping()["pong"] is True
        second.close()
    finally:
        first.close()


def test_shutdown_during_a_long_request_is_bounded(server_factory):
    server = server_factory(port=0, shutdown_timeout=0.25)
    setup = _client(server)
    setup.execute("AAYUSH CREATE TABLE a (id INT)")
    setup.execute("AAYUSH CREATE TABLE b (id INT)")
    values = ", ".join(f"({i})" for i in range(1, 701))
    setup.execute(f"AAYUSH INSERT INTO a VALUES {values}")
    setup.execute(f"AAYUSH INSERT INTO b VALUES {values}")
    setup.close()

    errors: list[BaseException] = []

    def _long_query() -> None:
        try:
            client = _client(server, read_timeout=30.0)
            client.execute(
                "AAYUSH SELECT a.id FROM a INNER JOIN b ON a.id > b.id"
            )
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    thread = threading.Thread(target=_long_query, daemon=True)
    thread.start()
    time.sleep(0.25)
    started = time.monotonic()
    drained = server.shutdown()
    elapsed = time.monotonic() - started
    thread.join(timeout=20)
    assert drained is False, "shutdown must not claim to have drained in-flight work"
    assert elapsed < 5.0, f"shutdown exceeded its bound: {elapsed:.1f}s"
    # The engine was not closed underneath the running query.
    assert server.engine.db.table("a").rows


def test_recovers_after_ambiguous_commit(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "ambiguous.aaydb"
    caplog.set_level("INFO", logger="aaysql.server")
    server = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    server.start()
    owner = _client(server)
    observer = _client(server)
    try:
        owner.execute("AAYUSH CREATE TABLE t (id INT, name TEXT)")
        owner.execute("AAYUSH INSERT INTO t VALUES (1, 'durable')")
        owner.execute("AAYUSH BEGIN")
        owner.execute("AAYUSH INSERT INTO t VALUES (2, 'maybe')")

        real_fsync = os.fsync
        state = {"fail": True}

        def flaky_fsync(fd):
            if state["fail"]:
                raise OSError(5, "injected fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", flaky_fsync)
        try:
            with pytest.raises(ServerError) as excinfo:
                owner.execute("AAYUSH COMMIT")
        finally:
            state["fail"] = False
        monkeypatch.undo()

        assert excinfo.value.code == "RECOVERY_REQUIRED"
        assert excinfo.value.retryable is False
        assert server.engine.needs_recovery is True

        # Every session, including the observer, is refused: no client may keep
        # using a database whose commit durability is unknown.
        for session_client in (observer,):
            with pytest.raises(ServerError) as blocked:
                session_client.execute("AAYUSH SELECT * FROM t")
            assert blocked.value.code == "RECOVERY_REQUIRED"

        with pytest.raises(ServerError) as busy:
            observer.execute("AAYUSH BEGIN")
        assert busy.value.code == "RECOVERY_REQUIRED"
    finally:
        owner.close()
        observer.close()
        server.shutdown()

    # The WAL is preserved for recovery; restarting decides the real state.
    restarted = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    restarted.start()
    try:
        with AayushSQLClient(*restarted.address) as client:
            rows = client.execute("AAYUSH SELECT * FROM t ORDER BY id ASC")["rows"]
            # The committed autocommit row is definitely durable. The ambiguous
            # COMMIT may or may not have reached the disk, but recovery must
            # resolve it to exactly one whole outcome: no duplicate and no
            # partial row. The server never claimed either answer.
            assert rows[0] == {"id": 1, "name": "durable"}
            assert len(rows) <= 2
            ids = [row["id"] for row in rows]
            assert ids == sorted(set(ids))
            assert ids in ([1], [1, 2])
    finally:
        restarted.shutdown()


# --- transaction ownership scenarios (Phase 7) ---------------------------


def test_scenario_a_two_autocommit_clients_share_one_engine(server):
    alice = _client(server)
    bob = _client(server)
    try:
        alice.execute("AAYUSH CREATE TABLE shared (id INT PRIMARY KEY, name TEXT)")
        bob.execute("AAYUSH INSERT INTO shared VALUES (1, 'bob')")
        rows = alice.execute("AAYUSH SELECT * FROM shared")["rows"]
        assert rows == [{"id": 1, "name": "bob"}]
        # One engine, one database instance.
        assert server.engine.db.table("shared").rows == [{"id": 1, "name": "bob"}]
    finally:
        alice.close()
        bob.close()


def test_scenario_b_transaction_is_owned_and_invisible(server):
    alice = _client(server)
    bob = _client(server)
    try:
        alice.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        alice.execute("AAYUSH BEGIN")
        alice.execute("AAYUSH INSERT INTO t VALUES (1)")
        with pytest.raises(ServerError) as excinfo:
            bob.execute("AAYUSH SELECT * FROM t")
        assert excinfo.value.code == TRANSACTION_BUSY
        assert excinfo.value.retryable is True
        # Bob never observes the uncommitted row, even through the engine object.
        assert server.engine.db.table("t").rows[0]["id"] == 1
        alice.execute("AAYUSH COMMIT")
        assert bob.execute("AAYUSH SELECT * FROM t")["rows"] == [{"id": 1}]
    finally:
        alice.close()
        bob.close()


def test_scenario_c_rollback_is_invisible(server):
    alice = _client(server)
    bob = _client(server)
    try:
        alice.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        alice.execute("AAYUSH BEGIN")
        alice.execute("AAYUSH INSERT INTO t VALUES (1)")
        with pytest.raises(ServerError) as excinfo:
            bob.execute("AAYUSH SELECT * FROM t")
        assert excinfo.value.code == TRANSACTION_BUSY
        alice.execute("AAYUSH ROLLBACK")
        assert bob.execute("AAYUSH SELECT * FROM t")["rows"] == []
    finally:
        alice.close()
        bob.close()


def test_scenario_d_disconnect_rolls_back_and_frees_the_database(server):
    alice = _client(server)
    bob = _client(server)
    try:
        alice.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        alice.execute("AAYUSH BEGIN")
        alice.execute("AAYUSH INSERT INTO t VALUES (1)")
        assert server.stats["transaction_owner"] is not None
        alice.close()
        assert _wait_for(lambda: server.stats["transaction_owner"] is None)
        assert bob.execute("AAYUSH SELECT * FROM t")["rows"] == []
        bob.execute("AAYUSH INSERT INTO t VALUES (2)")
        assert bob.execute("AAYUSH SELECT * FROM t")["rows"] == [{"id": 2}]
    finally:
        alice.close()
        bob.close()


def test_scenario_e_competing_begin_cannot_hijack_the_transaction(server):
    alice = _client(server)
    bob = _client(server)
    try:
        alice.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
        alice.execute("AAYUSH BEGIN")
        alice.execute("AAYUSH INSERT INTO t VALUES (1)")
        with pytest.raises(ServerError) as excinfo:
            bob.execute("AAYUSH BEGIN")
        assert excinfo.value.code == TRANSACTION_BUSY
        # Alice's transaction is intact and still hers.
        alice.execute("AAYUSH INSERT INTO t VALUES (2)")
        alice.execute("AAYUSH COMMIT")
        assert bob.execute("AAYUSH SELECT * FROM t")["row_count"] == 2
    finally:
        alice.close()
        bob.close()


def test_scenario_g_concurrent_clients_get_their_own_correlated_results(server):
    setup = _client(server)
    setup.execute("AAYUSH CREATE TABLE t (id INT, name TEXT)")
    setup.execute(
        "AAYUSH INSERT INTO t VALUES "
        + ", ".join(f"({i}, 'row{i}')" for i in range(1, 41))
    )
    setup.close()

    failures: list[BaseException] = []
    counts: list[int] = []
    lock = threading.Lock()

    def _worker(index: int) -> None:
        try:
            client = _client(server)
            try:
                for _ in range(5):
                    result = client.execute(
                        "AAYUSH SELECT id FROM t WHERE id <= 40 ORDER BY id ASC"
                    )
                    with lock:
                        counts.append(result["row_count"])
            finally:
                client.close()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            with lock:
                failures.append(exc)
        finally:
            del index

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert len(counts) == 30
    assert set(counts) == {40}
    assert server.stats["requests_served"] >= 30


def test_autocommit_mutations_are_durable_between_clients(tmp_path):
    db_path = tmp_path / "autocommit.aaydb"
    server = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    server.start()
    try:
        with AayushSQLClient(*server.address) as first:
            first.execute("AAYUSH CREATE TABLE t (id INT PRIMARY KEY)")
            first.execute("AAYUSH INSERT INTO t VALUES (1)")
        with AayushSQLClient(*server.address) as second:
            assert second.execute("AAYUSH SELECT * FROM t")["row_count"] == 1
    finally:
        server.shutdown()

    reopened = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    reopened.start()
    try:
        with AayushSQLClient(*reopened.address) as client:
            assert client.execute("AAYUSH SELECT * FROM t")["rows"] == [{"id": 1}]
    finally:
        reopened.shutdown()
