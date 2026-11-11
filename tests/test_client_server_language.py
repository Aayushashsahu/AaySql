"""Real-socket integration tests for the v0.9.0 SQL language over the v1 protocol.

The server speaks the same SQL strings as the embedded engine, so every test here
compares a client result with the embedded engine running the identical
statement on identical data. Network behaviour (error codes, connection
survival, transactions) is checked separately.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.client import AayushSQLClient, ServerError
from aaysql.engine import Engine
from aaysql.protocol import PROTOCOL_VERSION, SQL_EXECUTION_ERROR, SQL_PARSE_ERROR
from aaysql.server import AayushSQLServer, ServerConfig

SCHEMA = [
    "AAYUSH CREATE TABLE t (id INT, name TEXT, dept TEXT, score INT, price FLOAT, active BOOL)",
    """
    AAYUSH INSERT INTO t VALUES
        (1, 'Alex',  'Eng',   90, 10.5, TRUE),
        (2, 'Bob',   'Sales', 75, NULL, FALSE),
        (3, NULL,    'Eng',   NULL, 3.0, NULL),
        (4, 'Amy',   NULL,    60, 7.25, TRUE),
        (5, 'Zoe',   'Sales', 75, 1.0, FALSE)
    """,
]

QUERIES = [
    "AAYUSH SELECT id FROM t WHERE score IS NULL ORDER BY id",
    "AAYUSH SELECT id FROM t WHERE score IS NOT NULL AND dept IS NOT NULL ORDER BY id",
    "AAYUSH SELECT name FROM t WHERE name LIKE 'A%' ORDER BY name",
    "AAYUSH SELECT name FROM t WHERE name NOT LIKE '%o%' ORDER BY id",
    "AAYUSH SELECT id FROM t WHERE id IN (1, 3, 99) ORDER BY id",
    "AAYUSH SELECT id FROM t WHERE dept NOT IN ('Eng') ORDER BY id",
    "AAYUSH SELECT id, score FROM t WHERE score BETWEEN 60 AND 90 ORDER BY id",
    "AAYUSH SELECT id, score FROM t WHERE score NOT BETWEEN 60 AND 90 ORDER BY id",
    "AAYUSH SELECT id, score * 2 + 1 AS bumped FROM t ORDER BY id",
    "AAYUSH SELECT id, price - 1 AS cheaper FROM t ORDER BY id",
    (
        "AAYUSH SELECT id, CASE WHEN score IS NULL THEN 'none' WHEN score >= 80 "
        "THEN 'high' ELSE 'low' END AS band FROM t ORDER BY id"
    ),
    (
        "AAYUSH SELECT id, CASE dept WHEN 'Eng' THEN 1 WHEN 'Sales' THEN 2 ELSE 0 END "
        "AS grp FROM t ORDER BY id"
    ),
    "AAYUSH SELECT DISTINCT dept FROM t ORDER BY dept",
    "AAYUSH SELECT DISTINCT score FROM t ORDER BY score DESC LIMIT 2 OFFSET 1",
    (
        "AAYUSH SELECT dept, COUNT(*) AS c, SUM(score * 2) AS doubled FROM t "
        "GROUP BY dept ORDER BY dept"
    ),
    (
        "AAYUSH SELECT dept FROM t WHERE name LIKE '%e%' GROUP BY dept "
        "HAVING COUNT(*) BETWEEN 1 AND 2 ORDER BY dept"
    ),
    (
        "AAYUSH SELECT id FROM t WHERE (score IS NULL OR score > 70) AND name LIKE 'A%' "
        "ORDER BY id LIMIT 1 OFFSET 1"
    ),
    "AAYUSH SELECT 2 + 3 * 4 AS precedence FROM t WHERE id = 1",
]


@pytest.fixture()
def server():
    srv = AayushSQLServer(
        ServerConfig(
            db_path=None,
            port=0,
            read_timeout=5.0,
            write_timeout=5.0,
            idle_timeout=30.0,
            shutdown_timeout=5.0,
        )
    )
    srv.start()
    yield srv
    srv.shutdown()


@pytest.fixture()
def client(server):
    with AayushSQLClient(*server.address) as connected:
        for sql in SCHEMA:
            connected.execute(sql)
        yield connected


def _embedded() -> Engine:
    e = Engine()
    for sql in SCHEMA:
        e.execute(sql)
    return e


def test_protocol_version_is_unchanged():
    assert PROTOCOL_VERSION == 1


def _result_shape(result: dict) -> dict:
    """The client returns the embedded payload without the transport-level status."""
    return {
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
    }


@pytest.mark.parametrize("sql", QUERIES)
def test_client_matches_embedded_engine(client, sql):
    embedded = _embedded()
    assert _result_shape(client.execute(sql)) == _result_shape(embedded.execute(sql)), sql


def test_client_reports_the_same_columns_and_null_values(client):
    result = client.execute("AAYUSH SELECT id, price IS NULL AS missing FROM t ORDER BY id")
    assert result["columns"] == ["id", "missing"]
    assert result["rows"] == [
        {"id": 1, "missing": False},
        {"id": 2, "missing": True},
        {"id": 3, "missing": False},
        {"id": 4, "missing": False},
        {"id": 5, "missing": False},
    ]


def test_parser_errors_use_a_stable_code_and_keep_the_connection(client):
    for sql, code in [
        ("AAYUSH SELECT * FROM t WHERE id IN ()", SQL_PARSE_ERROR),
        ("AAYUSH SELECT CASE WHEN id THEN 1 FROM t", SQL_PARSE_ERROR),
        ("AAYUSH SELECT DISTINCT DISTINCT id FROM t", SQL_PARSE_ERROR),
        ("AAYUSH SELECT * FROM t LIMIT -1", SQL_PARSE_ERROR),
        ("AAYUSH SELECT * FROM t OFFSET -1", SQL_PARSE_ERROR),
    ]:
        with pytest.raises(ServerError) as excinfo:
            client.execute(sql)
        assert excinfo.value.code == code, sql
    assert client.ping().get("operation", True)
    assert client.execute("AAYUSH SELECT COUNT(*) AS c FROM t")["rows"] == [{"c": 5}]


def test_execution_errors_use_a_stable_code_and_keep_the_connection(client):
    for sql, message in [
        ("AAYUSH SELECT 1 / 0 AS r FROM t", "Division by zero"),
        ("AAYUSH SELECT 1 % 0 AS r FROM t", "Modulo by zero"),
        ("AAYUSH SELECT name + 1 AS r FROM t", "requires numeric operands"),
    ]:
        with pytest.raises(ServerError) as excinfo:
            client.execute(sql)
        assert excinfo.value.code == SQL_EXECUTION_ERROR, sql
        if message:
            assert message in str(excinfo.value)
    assert client.execute("AAYUSH SELECT COUNT(*) AS c FROM t")["rows"] == [{"c": 5}]


def test_transactions_with_expressions_are_visible_only_inside_the_session(server):
    with AayushSQLClient(*server.address) as first:
        for sql in SCHEMA:
            first.execute(sql)
        first.execute("AAYUSH BEGIN")
        first.execute("AAYUSH UPDATE t SET score = score * 2 WHERE score IS NOT NULL")
        first.execute(
            "AAYUSH UPDATE t SET name = CASE WHEN score > 150 THEN 'big' ELSE name END"
        )
        assert first.execute("AAYUSH SELECT id, score FROM t WHERE id = 1")["rows"] == [
            {"id": 1, "score": 180}
        ]
        # Another session cannot see or join the in-progress transaction.
        with AayushSQLClient(*server.address) as second:
            with pytest.raises(ServerError) as excinfo:
                second.execute("AAYUSH SELECT COUNT(*) AS c FROM t")
            assert excinfo.value.code == "TRANSACTION_BUSY"
        first.execute("AAYUSH ROLLBACK")
        assert first.execute("AAYUSH SELECT id, score FROM t WHERE id = 1")["rows"] == [
            {"id": 1, "score": 90}
        ]


def test_arithmetic_failure_inside_a_transaction_is_statement_atomic(client):
    client.execute("AAYUSH BEGIN")
    client.execute("AAYUSH INSERT INTO t VALUES (6, 'New', 'Eng', 50, 0.0, TRUE)")
    with pytest.raises(ServerError) as excinfo:
        client.execute("AAYUSH UPDATE t SET price = 100 / price")
    assert excinfo.value.code == SQL_EXECUTION_ERROR
    assert "Division by zero" in str(excinfo.value)
    # The failed statement changed nothing; the earlier INSERT is still pending.
    assert client.execute("AAYUSH SELECT id, price FROM t WHERE id IN (2, 6) ORDER BY id")["rows"] == [
        {"id": 2, "price": None},
        {"id": 6, "price": 0.0},
    ]
    client.execute("AAYUSH COMMIT")
    assert client.execute("AAYUSH SELECT COUNT(*) AS c FROM t")["rows"] == [{"c": 6}]


def test_expression_results_survive_a_server_restart(tmp_path):
    path = tmp_path / "server-lang.aaydb"
    config = {
        "db_path": str(path),
        "port": 0,
        "read_timeout": 5.0,
        "write_timeout": 5.0,
        "idle_timeout": 30.0,
        "shutdown_timeout": 5.0,
    }
    first = AayushSQLServer(ServerConfig(**config))
    first.start()
    with AayushSQLClient(*first.address) as client:
        for sql in SCHEMA:
            client.execute(sql)
        client.execute("AAYUSH UPDATE t SET price = price + 1 WHERE price IS NOT NULL")
    first.shutdown()

    second = AayushSQLServer(ServerConfig(**config))
    second.start()
    try:
        with AayushSQLClient(*second.address) as client:
            assert client.execute("AAYUSH SELECT id, price FROM t WHERE price IS NOT NULL ORDER BY id")[
                "rows"
            ] == [
                {"id": 1, "price": 11.5},
                {"id": 3, "price": 4.0},
                {"id": 4, "price": 8.25},
                {"id": 5, "price": 2.0},
            ]
            assert client.execute("AAYUSH SELECT DISTINCT dept FROM t ORDER BY dept")["rows"] == [
                {"dept": "Eng"},
                {"dept": "Sales"},
                {"dept": None},
            ]
    finally:
        second.shutdown()
