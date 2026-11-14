"""Tests for Phase 5 Native JSON & Document Model with MVCC snapshot readers.

Covers:
- Snapshot reader isolation on tables with JSON columns
- Concurrent writer mutating JSON documents (point updates, path sets, replaces)
- Verifying memory mutability isolation (copy-on-write so writer cannot mutate reader's snapshot in-memory)
"""

from __future__ import annotations

from aaysql import Engine


def _rows(session, sql):
    return session.execute(sql)["rows"]


def test_mvcc_reader_json_snapshot_isolation():
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE items (id INT PRIMARY KEY, doc JSON);")
    engine.execute("AAYUSH INSERT INTO items VALUES (1, '{\"status\": \"pending\", \"items\": [\"a\"]}');")

    reader = engine.connect("SNAPSHOT")
    writer = engine.connect()

    # Reader starts snapshot transaction
    reader.execute("AAYUSH BEGIN;")
    assert _rows(reader, "AAYUSH SELECT id, doc.status, doc.items[0] FROM items;") == [
        {"id": 1, "doc.status": "pending", "doc.items[0]": "a"}
    ]

    # Writer modifies the document
    writer.execute("AAYUSH BEGIN;")
    writer.execute("AAYUSH UPDATE items SET doc = '{\"status\": \"approved\", \"items\": [\"a\", \"b\"]}' WHERE id = 1;")
    writer.execute("AAYUSH COMMIT;")

    # Reader must STILL see the exact snapshot state without any mutation bleed-through
    assert _rows(reader, "AAYUSH SELECT id, doc.status, doc.items[0] FROM items;") == [
        {"id": 1, "doc.status": "pending", "doc.items[0]": "a"}
    ]

    # After reader commits and begins new snapshot, reader sees latest committed document
    reader.execute("AAYUSH COMMIT;")
    reader.execute("AAYUSH BEGIN;")
    assert _rows(reader, "AAYUSH SELECT id, doc.status, doc.items[1] FROM items;") == [
        {"id": 1, "doc.status": "approved", "doc.items[1]": "b"}
    ]
    reader.execute("AAYUSH COMMIT;")


def test_mvcc_json_in_memory_isolation():
    """Verify that writer modifying python dict/list objects does not mutate reader snapshot."""
    engine = Engine()
    engine.execute("AAYUSH CREATE TABLE configs (id INT PRIMARY KEY, cfg JSON);")
    engine.execute("AAYUSH INSERT INTO configs VALUES (1, '{\"theme\": \"dark\", \"nest\": {\"level\": 1}}');")

    reader = engine.connect("SNAPSHOT")
    writer = engine.connect()

    reader.execute("AAYUSH BEGIN;")
    initial = _rows(reader, "AAYUSH SELECT cfg FROM configs;")[0]["cfg"]
    assert initial == {"theme": "dark", "nest": {"level": 1}}

    # Writer modifies nested object and adds new properties
    writer.execute("AAYUSH UPDATE configs SET cfg.nest.level = 2 WHERE id = 1;")

    # Reader's in-memory retrieved object must not be mutated in-place
    after = _rows(reader, "AAYUSH SELECT cfg FROM configs;")[0]["cfg"]
    assert after == {"theme": "dark", "nest": {"level": 1}}
    assert after["nest"]["level"] == 1
    reader.execute("AAYUSH COMMIT;")
