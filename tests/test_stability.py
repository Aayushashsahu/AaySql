"""Regression tests for defects found during the v1.0.0 stabilization audit.

Each test here reproduces a concrete problem that existed before the fix:
a Python-only comparison leaking into SQL, a misleading protocol error code for
an oversized client request, and duplicate ``auth_token`` construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql.client import AayushSQLClient, ProtocolError
from aaysql.engine import Engine
from aaysql.errors import ExecutionError
from aaysql.protocol import AUTH_TOKEN_ENV, REQUEST_TOO_LARGE
from aaysql.server import ServerConfig, ServerConfigError


def _engine() -> Engine:
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE t (id INT, flag BOOL, score INT, name TEXT)")
    engine.execute(
        "AAYUSH INSERT INTO t VALUES (1, TRUE, 10, 'a'), (2, FALSE, 20, 'b')"
    )
    return engine


# --- BOOL comparison must not follow Python equality -----------------------


def test_boolean_equal_to_integer_is_rejected():
    engine = _engine()
    with pytest.raises(ExecutionError, match="Cannot compare BOOL with INT"):
        engine.execute("AAYUSH SELECT id FROM t WHERE flag = 1")


def test_integer_equal_to_boolean_is_rejected():
    engine = _engine()
    with pytest.raises(ExecutionError, match="Cannot compare BOOL with INT"):
        engine.execute("AAYUSH SELECT id FROM t WHERE 0 = flag")


def test_boolean_inequality_with_integer_is_rejected():
    engine = _engine()
    with pytest.raises(ExecutionError, match="Cannot compare BOOL with INT"):
        engine.execute("AAYUSH SELECT id FROM t WHERE flag != 0")


def test_boolean_ordering_against_integer_is_rejected():
    engine = _engine()
    with pytest.raises(ExecutionError, match="Cannot compare BOOL with INT"):
        engine.execute("AAYUSH SELECT id FROM t WHERE flag < 1")


def test_boolean_to_boolean_comparison_still_works():
    engine = _engine()
    rows = engine.execute("AAYUSH SELECT id FROM t WHERE flag = TRUE")["rows"]
    assert rows == [{"id": 1}]
    rows = engine.execute("AAYUSH SELECT id FROM t WHERE flag = FALSE")["rows"]
    assert rows == [{"id": 2}]
    rows = engine.execute("AAYUSH SELECT id FROM t WHERE flag < TRUE")["rows"]
    assert rows == [{"id": 2}]


def test_null_operand_comparison_stays_unknown():
    engine = _engine()
    engine.execute("AAYUSH INSERT INTO t VALUES (3, TRUE, NULL, 'c')")
    # NULL = 1 is UNKNOWN, not an error: NULL never reaches the type check.
    rows = engine.execute("AAYUSH SELECT id FROM t WHERE score = 1 OR id = 3")["rows"]
    assert rows == [{"id": 3}]


def test_numeric_and_text_comparisons_keep_sql_behavior():
    engine = _engine()
    # INT/FLOAT compare numerically; TEXT vs numeric is simply unequal.
    assert engine.execute("AAYUSH SELECT id FROM t WHERE score = 10.0")["rows"] == [
        {"id": 1}
    ]
    assert engine.execute("AAYUSH SELECT id FROM t WHERE name = 10")["rows"] == []


# --- client-side oversized request is labelled correctly -------------------


class _DummySocket:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:  # pragma: no cover - never reached
        self.sent += data

    def settimeout(self, value: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_oversized_request_reports_request_too_large():
    client = AayushSQLClient(max_frame_size=64)
    client._socket = _DummySocket()  # noqa: SLF001 - deliberate low-level probe
    with pytest.raises(ProtocolError) as excinfo:
        client.execute("AAYUSH SELECT '" + "x" * 500 + "'")
    assert REQUEST_TOO_LARGE in str(excinfo.value)
    assert "request" in str(excinfo.value)
    assert not client.connected


def test_send_frame_default_still_labels_responses():
    from aaysql.protocol import RESPONSE_TOO_LARGE, FrameError, send_frame

    with pytest.raises(FrameError) as excinfo:
        send_frame(_DummySocket(), {"status": "ok", "data": "y" * 200}, 32)
    assert excinfo.value.code == RESPONSE_TOO_LARGE
    assert "response" in excinfo.value.message


# --- configuration construction -------------------------------------------


def test_auth_token_from_environment_wins_without_duplicate_keyword(monkeypatch):
    monkeypatch.setenv(AUTH_TOKEN_ENV, "env-token")
    config = ServerConfig.from_environment(auth_token="explicit-token")
    assert config.auth_token == "env-token"


def test_explicit_token_used_when_environment_is_unset(monkeypatch):
    monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
    config = ServerConfig.from_environment(auth_token="explicit-token")
    assert config.auth_token == "explicit-token"


def test_empty_token_environment_is_rejected(monkeypatch):
    monkeypatch.setenv(AUTH_TOKEN_ENV, "   ")
    with pytest.raises(ServerConfigError, match="set but empty"):
        ServerConfig.from_environment()
