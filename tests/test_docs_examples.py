"""Verify that every SQL, client/server, and backup example in docs/GETTING_STARTED.md works."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql import Engine
from aaysql.backup import backup_database, restore_database, verify_backup
from aaysql.client import AayushSQLClient
from aaysql.server import AayushSQLServer, ServerConfig


def test_getting_started_examples_end_to_end(tmp_path: Path):
    db_path = tmp_path / "university.aaydb"
    engine = Engine(db_path)
    try:
        # Section 2: Create tables
        engine.execute(
            """
            AAYUSH CREATE TABLE departments (
                id INT PRIMARY KEY,
                name TEXT NOT NULL
            )
            """
        )
        engine.execute(
            """
            AAYUSH CREATE TABLE students (
                id INT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE,
                department_id INT,
                score INT,
                enrolled BOOL DEFAULT TRUE
            )
            """
        )

        # Section 3: Insert rows
        engine.execute(
            "AAYUSH INSERT INTO departments VALUES (1, 'Engineering'), (2, 'Design')"
        )
        engine.execute(
            """
            AAYUSH INSERT INTO students (id, name, email, department_id, score)
            VALUES (1, 'Ada', 'ada@example.com', 1, 92),
                   (2, 'Grace', 'grace@example.com', 1, 88),
                   (3, 'Alan', 'alan@example.com', 2, 74),
                   (4, 'Edsger', NULL, 2, NULL)
            """
        )

        # Section 4: Query
        assert len(
            engine.execute(
                "AAYUSH SELECT id, name, score FROM students WHERE score >= 80 ORDER BY score DESC"
            )["rows"]
        ) == 2
        assert engine.execute(
            "AAYUSH SELECT name FROM students WHERE email IS NULL"
        )["rows"] == [{"name": "Edsger"}]
        assert len(
            engine.execute("AAYUSH SELECT name FROM students WHERE name LIKE 'A%'")[
                "rows"
            ]
        ) == 2
        assert len(
            engine.execute(
                "AAYUSH SELECT name FROM students WHERE score BETWEEN 70 AND 90"
            )["rows"]
        ) == 2
        assert len(
            engine.execute("AAYUSH SELECT name FROM students WHERE department_id IN (1, 3)")[
                "rows"
            ]
        ) == 2

        # Section 5: Joins
        joined = engine.execute(
            """
            AAYUSH SELECT s.name AS student, d.name AS department
            FROM students AS s
            JOIN departments AS d ON s.department_id = d.id
            ORDER BY student
            """
        )
        assert joined["row_count"] == 4
        left_null = engine.execute(
            """
            AAYUSH SELECT s.name AS student, d.name AS department
            FROM students AS s
            LEFT JOIN departments AS d ON s.department_id = d.id
            WHERE d.name IS NULL
            """
        )
        assert left_null["rows"] == []

        # Section 6: Aggregate and group
        agg = engine.execute(
            "AAYUSH SELECT COUNT(*) AS total, AVG(score) AS average FROM students"
        )
        assert agg["rows"][0]["total"] == 4
        grp = engine.execute(
            """
            AAYUSH SELECT department_id, COUNT(*) AS students, MAX(score) AS best
            FROM students
            GROUP BY department_id
            HAVING COUNT(*) > 1
            ORDER BY students DESC, department_id
            """
        )
        assert grp["row_count"] == 2

        # Section 7: Indexes and EXPLAIN
        engine.execute("AAYUSH CREATE INDEX idx_students_score ON students (score)")
        explain = engine.execute("AAYUSH EXPLAIN SELECT id FROM students WHERE score = 88")
        assert "students" in explain["message"]
        analyze = engine.execute(
            "AAYUSH EXPLAIN ANALYZE SELECT id FROM students WHERE score BETWEEN 70 AND 90"
        )
        assert "ROWS RETURNED: 2" in analyze["message"]

        # Section 8: Expressions, CASE, DISTINCT, OFFSET
        boosted = engine.execute(
            "AAYUSH SELECT name, score, score + 5 AS boosted FROM students ORDER BY id"
        )
        assert boosted["rows"][0]["boosted"] == 97
        banded = engine.execute(
            """
            AAYUSH SELECT name,
                   CASE WHEN score IS NULL THEN 'unknown'
                        WHEN score >= 90 THEN 'excellent'
                        WHEN score >= 80 THEN 'good'
                        ELSE 'review' END AS band
            FROM students
            ORDER BY id
            """
        )
        assert [r["band"] for r in banded["rows"]] == [
            "excellent",
            "good",
            "review",
            "unknown",
        ]
        distinct_depts = engine.execute(
            "AAYUSH SELECT DISTINCT department_id FROM students ORDER BY department_id"
        )
        assert distinct_depts["rows"] == [{"department_id": 1}, {"department_id": 2}]
        offset_rows = engine.execute(
            "AAYUSH SELECT name FROM students ORDER BY score DESC, id LIMIT 2 OFFSET 1"
        )
        assert len(offset_rows["rows"]) == 2

        # Section 9: Transactions
        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH UPDATE students SET score = 95 WHERE id = 1")
        engine.execute(
            "AAYUSH INSERT INTO students (id, name, department_id, score) VALUES (5, 'Barbara', 1, 81)"
        )
        engine.execute("AAYUSH COMMIT")

        engine.execute("AAYUSH BEGIN")
        engine.execute("AAYUSH DELETE FROM students WHERE id = 5")
        engine.execute("AAYUSH ROLLBACK")
    finally:
        engine.close()

    # Section 10: Persistence
    reopened = Engine(db_path)
    try:
        assert reopened.execute("AAYUSH SELECT COUNT(*) FROM students")["rows"] == [
            {"COUNT(*)": 5}
        ]
    finally:
        reopened.close()

    # Section 11: Client/server mode
    server = AayushSQLServer(ServerConfig(db_path=db_path, port=0))
    server.start()
    try:
        with AayushSQLClient(*server.address) as client:
            res = client.execute("AAYUSH SELECT name, score FROM students ORDER BY id")
            assert res["row_count"] == 5
    finally:
        server.shutdown()

    # Section 12: Backup and restore
    backup_dir = tmp_path / "backups" / "university-backup"
    manifest = backup_database(db_path, backup_dir)
    assert verify_backup(backup_dir)["database_sha256"] == manifest["database_sha256"]
    restored_path = tmp_path / "university-restored.aaydb"
    restore_database(backup_dir, restored_path)
    with Engine(restored_path) as restored_engine:
        assert restored_engine.execute("AAYUSH SELECT COUNT(*) FROM students")["rows"] == [
            {"COUNT(*)": 5}
        ]
