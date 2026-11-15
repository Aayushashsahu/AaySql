"""Planner and optimization tests for JSON path indexes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import PlanNode, format_plan, plan_select


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _reference(engine: Engine, sql: str):
    stmt = _stmt(sql)
    plan = plan_select(engine.db, stmt, force_access_path="TABLE")
    return execute_select(engine.db, stmt, plan=plan)


def _find_scan(root: PlanNode, ref: str) -> PlanNode:
    for node in _walk(root):
        if node.op in ("TABLE SCAN", "INDEX SCAN") and node.ref == ref:
            return node
    raise AssertionError(f"no scan for source '{ref}'")


def _walk(node: PlanNode):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_path_index_matching_and_explain(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE TABLE events (id INT, payload JSON);")
    engine.execute("AAYUSH CREATE INDEX idx_events_user_id ON events (payload.user.id);")

    for i in range(1, 101):
        engine.execute(
            f"AAYUSH INSERT INTO events VALUES ({i}, '{{\"user\": {{\"id\": {i}, \"role\": \"admin\"}}}}');"
        )

    # Test equality match
    plan = plan_select(engine.db, _stmt("AAYUSH SELECT * FROM events WHERE payload.user.id = 42;"))
    assert plan.root is not None
    scan = _find_scan(plan.root, "events")
    assert scan.op == "INDEX SCAN"
    assert scan.index_name == "idx_events_user_id"
    assert scan.index_path == ("user", "id")

    formatted = format_plan(plan)
    assert "INDEX SCAN: idx_events_user_id" in formatted
    assert "TABLE: events" in formatted
    assert "PATH: payload.user.id" in formatted

    explain_res = engine.execute("AAYUSH EXPLAIN SELECT * FROM events WHERE payload.user.id = 42;")
    assert "INDEX SCAN: idx_events_user_id" in explain_res["message"]
    assert "PATH: payload.user.id" in explain_res["message"]


def test_path_index_range_predicates_and_differential(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE TABLE users (id INT, profile JSON);")
    engine.execute("AAYUSH CREATE INDEX idx_profile_score ON users (profile.score);")

    # Insert 100 rows with varying scores, some null, some missing score, some non-numeric
    for i in range(1, 101):
        if i % 10 == 0:
            engine.execute(f"AAYUSH INSERT INTO users VALUES ({i}, '{{\"name\": \"user{i}\"}}');")
        elif i % 10 == 1:
            engine.execute(f"AAYUSH INSERT INTO users VALUES ({i}, '{{\"name\": \"user{i}\", \"score\": null}}');")
        else:
            engine.execute(f"AAYUSH INSERT INTO users VALUES ({i}, '{{\"name\": \"user{i}\", \"score\": {i * 1.5}}}');")

    queries = [
        "AAYUSH SELECT * FROM users WHERE profile.score = 30.0;",
        "AAYUSH SELECT * FROM users WHERE profile.score > 50;",
        "AAYUSH SELECT * FROM users WHERE profile.score >= 50.0;",
        "AAYUSH SELECT * FROM users WHERE profile.score < 25;",
        "AAYUSH SELECT * FROM users WHERE profile.score <= 25.5;",
        "AAYUSH SELECT * FROM users WHERE profile.score BETWEEN 20 AND 60;",
        "AAYUSH SELECT * FROM users WHERE profile.score > 20 AND profile.name = 'user30';",
    ]

    for q in queries:
        opt_res = engine.execute(q)
        ref_res = _reference(engine, q)
        assert opt_res["rows"] == ref_res["rows"], f"Mismatch for query: {q}"

        plan = plan_select(engine.db, _stmt(q))
        assert plan.root is not None
        scan = _find_scan(plan.root, "users")
        assert scan.op == "INDEX SCAN", f"Expected INDEX SCAN for query: {q}"
        assert scan.index_name == "idx_profile_score"


def test_path_index_not_used_when_unindexed_path(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE TABLE items (id INT, data JSON);")
    engine.execute("AAYUSH CREATE INDEX idx_items_price ON items (data.price);")

    engine.execute("AAYUSH INSERT INTO items VALUES (1, '{\"price\": 10, \"rating\": 4.5}');")
    engine.execute("AAYUSH INSERT INTO items VALUES (2, '{\"price\": 20, \"rating\": 3.5}');")

    # Query on data.rating should not match idx_items_price
    plan = plan_select(engine.db, _stmt("AAYUSH SELECT * FROM items WHERE data.rating = 4.5;"))
    assert plan.root is not None
    scan = _find_scan(plan.root, "items")
    assert scan.op == "TABLE SCAN"


def test_path_index_collection_shorthand(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE COLLECTION customers;")
    engine.execute("AAYUSH CREATE INDEX idx_cust_city ON customers (body.address.city);")

    engine.execute("AAYUSH INSERT INTO customers (body) VALUES ('{\"name\": \"Alice\", \"address\": {\"city\": \"London\"}}');")
    engine.execute("AAYUSH INSERT INTO customers (body) VALUES ('{\"name\": \"Bob\", \"address\": {\"city\": \"Paris\"}}');")
    engine.execute("AAYUSH INSERT INTO customers (body) VALUES ('{\"name\": \"Charlie\", \"address\": {\"city\": \"London\"}}');")

    res = engine.execute("AAYUSH SELECT * FROM customers WHERE body.address.city = 'London';")
    assert len(res["rows"]) == 2

    plan = plan_select(engine.db, _stmt("AAYUSH SELECT * FROM customers WHERE body.address.city = 'London';"))
    assert plan.root is not None
    scan = _find_scan(plan.root, "customers")
    assert scan.op == "INDEX SCAN"
    assert scan.index_name == "idx_cust_city"
