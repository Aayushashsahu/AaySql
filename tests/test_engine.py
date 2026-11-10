import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql import Engine
from aaysql.errors import AayushSQLError, ConstraintError, ParserError


def test_crud():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)")
    e.execute("AAYUSH INSERT INTO users VALUES (1, 'Aayush', 19)")
    e.execute("AAYUSH INSERT INTO users VALUES (2, 'Mika', 21)")
    r = e.execute("AAYUSH SELECT name, age FROM users WHERE age >= 20 ORDER BY age DESC")
    assert r["rows"] == [{"name": "Mika", "age": 21}]
    e.execute("AAYUSH UPDATE users SET age = 20 WHERE id = 1")
    e.execute("AAYUSH DELETE FROM users WHERE id = 2")
    assert e.execute("AAYUSH SELECT * FROM users")["rows"] == [{"id": 1, "name": "Aayush", "age": 20}]


def test_prefix_required():
    e = Engine()
    with pytest.raises(ParserError):
        e.execute("SELECT * FROM users")


def test_duplicate_prefix():
    e = Engine()
    with pytest.raises(ParserError, match="Duplicate AAYUSH"):
        e.execute("AAYUSH AAYUSH SELECT * FROM users")


def test_primary_key():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
    e.execute("AAYUSH INSERT INTO users VALUES (1, 'A')")
    with pytest.raises(ConstraintError):
        e.execute("AAYUSH INSERT INTO users VALUES (1, 'B')")
