"""Tests for cross-model relational / document joins."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.engine import Engine
from aaysql.execution import execute_select
from aaysql.lexer import Lexer
from aaysql.parser import Parser
from aaysql.planner import plan_select


def _stmt(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _reference(engine: Engine, sql: str):
    stmt = _stmt(sql)
    plan = plan_select(engine.db, stmt, force_join_algorithm="NESTED LOOP", force_access_path="TABLE")
    return execute_select(engine.db, stmt, plan=plan)


def test_cross_model_inner_join_basic(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE TABLE users (id INT, name TEXT);")
    engine.execute("AAYUSH CREATE COLLECTION events;")

    engine.execute("AAYUSH INSERT INTO users VALUES (1, 'Alice');")
    engine.execute("AAYUSH INSERT INTO users VALUES (2, 'Bob');")
    engine.execute("AAYUSH INSERT INTO users VALUES (3, 'Charlie');")

    engine.execute("AAYUSH INSERT INTO events (body) VALUES ('{\"user_id\": 1, \"action\": \"login\"}');")
    engine.execute("AAYUSH INSERT INTO events (body) VALUES ('{\"user_id\": 1, \"action\": \"view\"}');")
    engine.execute("AAYUSH INSERT INTO events (body) VALUES ('{\"user_id\": 2, \"action\": \"logout\"}');")
    engine.execute("AAYUSH INSERT INTO events (body) VALUES ('{\"user_id\": 99, \"action\": \"unknown\"}');")
    engine.execute("AAYUSH INSERT INTO events (body) VALUES ('{\"action\": \"anonymous\"}');")

    sql = "AAYUSH SELECT u.name, e.body.action FROM users u JOIN events e ON u.id = e.body.user_id;"
    opt_res = engine.execute(sql)
    ref_res = _reference(engine, sql)

    assert opt_res["rows"] == ref_res["rows"]
    assert len(opt_res["rows"]) == 3
    actions = [r["e.body.action"] for r in opt_res["rows"]]
    assert "login" in actions
    assert "view" in actions
    assert "logout" in actions


def test_cross_model_left_join(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE TABLE users (id INT, name TEXT);")
    engine.execute("AAYUSH CREATE COLLECTION events;")

    engine.execute("AAYUSH INSERT INTO users VALUES (1, 'Alice');")
    engine.execute("AAYUSH INSERT INTO users VALUES (2, 'Bob');")
    engine.execute("AAYUSH INSERT INTO users VALUES (3, 'Charlie');")  # Charlie has no events

    engine.execute("AAYUSH INSERT INTO events (body) VALUES ('{\"user_id\": 1, \"action\": \"login\"}');")

    sql = "AAYUSH SELECT u.name, e.body.action FROM users u LEFT JOIN events e ON u.id = e.body.user_id;"
    opt_res = engine.execute(sql)
    ref_res = _reference(engine, sql)

    assert opt_res["rows"] == ref_res["rows"]
    assert len(opt_res["rows"]) == 3
    charlie_row = next(r for r in opt_res["rows"] if r["u.name"] == "Charlie")
    from aaysql.json_value import ABSENT
    assert charlie_row["e.body.action"] in (None, ABSENT)


def test_cross_model_hash_join_and_differential_large(tmp_path):
    engine = Engine(tmp_path / "test.db")
    engine.execute("AAYUSH CREATE TABLE customers (id INT, tier TEXT);")
    engine.execute("AAYUSH CREATE COLLECTION orders;")

    # 100 customers
    for i in range(1, 101):
        tier = "gold" if i % 2 == 0 else "silver"
        engine.execute(f"AAYUSH INSERT INTO customers VALUES ({i}, '{tier}');")

    # 200 orders (some matching, some missing cust_id, some null cust_id)
    for j in range(1, 201):
        if j % 10 == 0:
            # Missing customer_id
            engine.execute(f"AAYUSH INSERT INTO orders (body) VALUES ('{{\"order_id\": {j}, \"amount\": {j * 10}}}');")
        elif j % 10 == 1:
            # Null customer_id
            engine.execute(f"AAYUSH INSERT INTO orders (body) VALUES ('{{\"customer_id\": null, \"order_id\": {j}}}');")
        else:
            cust_id = (j % 50) + 1  # maps to 1..50
            engine.execute(
                f"AAYUSH INSERT INTO orders (body) VALUES ('{{\"customer_id\": {cust_id}, \"order_id\": {j}, \"amount\": {j * 10}}}');"
            )

    queries = [
        "AAYUSH SELECT c.id, c.tier, o.body.order_id, o.body.amount FROM customers c JOIN orders o ON c.id = o.body.customer_id;",
        "AAYUSH SELECT c.id, c.tier, o.body.order_id FROM customers c LEFT JOIN orders o ON c.id = o.body.customer_id;",
        "AAYUSH SELECT c.tier, count(*) AS cnt FROM customers c JOIN orders o ON c.id = o.body.customer_id GROUP BY c.tier;",
    ]

    for q in queries:
        opt = engine.execute(q)
        ref = _reference(engine, q)
        assert opt["rows"] == ref["rows"], f"Mismatch for query: {q}"

    # Verify that planner used HASH JOIN for the large join
    plan = plan_select(engine.db, _stmt(queries[0]))
    assert plan.root is not None
    # Check if HASH JOIN node exists
    has_hash_join = any(node.algorithm == "HASH" for node in _walk_plan(plan.root))
    assert has_hash_join, "Expected HASH JOIN for 100x200 cross-model query"


def _walk_plan(node):
    yield node
    for child in node.children:
        yield from _walk_plan(child)
