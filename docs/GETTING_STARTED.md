# Getting Started with AayushSQL 1.0.0

AayushSQL is a relational database engine written in pure Python. Every SQL
statement must begin with the `AAYUSH` prefix; the SQL after it is deliberately
ordinary.

This tutorial creates a database, runs the language end to end, and finishes with a
server, a Python client, and a backup. Every statement below is executed by the test
suite, so the examples and the engine cannot drift apart.

---

## 1. Install

From a wheel (recommended):

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install aaysql-1.0.0-py3-none-any.whl
```

From the repository:

```bash
pip install -e .
```

Verify:

```bash
aaysql --version          # aaysql 1.0.0
aaysql-server --help
aaysql-backup --version
```

Python 3.10–3.13 are supported. There are no runtime dependencies.

---

## 2. Create a database and a table

The shell takes a database path. Omitting it uses `aaysql.aaydb` in the current
directory; `--memory` runs without touching disk.

```bash
aaysql --db university.aaydb
```

```sql
AAYUSH CREATE TABLE departments (
    id INT PRIMARY KEY,
    name TEXT NOT NULL
);

AAYUSH CREATE TABLE students (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    department_id INT,
    score INT,
    enrolled BOOL DEFAULT TRUE
);
```

Supported types: `INT`/`INTEGER`, `TEXT`/`VARCHAR`, `FLOAT`/`DOUBLE`,
`BOOL`/`BOOLEAN`. Supported column constraints: `PRIMARY KEY`, `NOT NULL`,
`UNIQUE`, `DEFAULT` (literal values only).

---

## 3. Insert rows

```sql
AAYUSH INSERT INTO departments VALUES (1, 'Engineering'), (2, 'Design');

AAYUSH INSERT INTO students (id, name, email, department_id, score)
VALUES (1, 'Ada', 'ada@example.com', 1, 92),
       (2, 'Grace', 'grace@example.com', 1, 88),
       (3, 'Alan', 'alan@example.com', 2, 74),
       (4, 'Edsger', NULL, 2, NULL);
```

Omitted columns take their `DEFAULT`, or `NULL` when the column is nullable.
`INSERT`, `UPDATE`, and `DELETE` are atomic: constraint or arithmetic failures leave
the table exactly as it was.

---

## 4. Query

```sql
AAYUSH SELECT id, name, score FROM students WHERE score >= 80 ORDER BY score DESC;

AAYUSH SELECT name FROM students WHERE email IS NULL;

AAYUSH SELECT name FROM students WHERE name LIKE 'A%';

AAYUSH SELECT name FROM students WHERE score BETWEEN 70 AND 90;

AAYUSH SELECT name FROM students WHERE department_id IN (1, 3);
```

`NULL` follows SQL three-valued logic: `score = NULL` is `UNKNOWN` (no rows),
`score IS NULL` is `TRUE` exactly for missing values. In `ORDER BY`, `NULL` sorts
last ascending and first descending.

---

## 5. Joins

```sql
AAYUSH SELECT s.name AS student, d.name AS department
FROM students AS s
JOIN departments AS d ON s.department_id = d.id
ORDER BY student;

AAYUSH SELECT s.name AS student, d.name AS department
FROM students AS s
LEFT JOIN departments AS d ON s.department_id = d.id
WHERE d.name IS NULL;
```

`JOIN` and `INNER JOIN` are the same. `LEFT JOIN` pads unmatched left rows with
`NULL`. With the rows above every student has a department, so that last query
returns no rows; give a student a `department_id` that does not exist to see the
padding. `RIGHT`, `FULL`, `CROSS`, and `NATURAL` joins are not supported and produce
a parse error.

---

## 6. Aggregate and group

```sql
AAYUSH SELECT COUNT(*) AS total, AVG(score) AS average FROM students;

AAYUSH SELECT department_id, COUNT(*) AS students, MAX(score) AS best
FROM students
GROUP BY department_id
HAVING COUNT(*) > 1
ORDER BY students DESC, department_id;
```

Aggregates: `COUNT(*)`, `COUNT(column)`, `SUM`, `AVG`, `MIN`, `MAX`. `GROUP BY`
accepts column references only.

---

## 7. Indexes and EXPLAIN

```sql
AAYUSH CREATE INDEX idx_students_score ON students (score);

AAYUSH EXPLAIN SELECT id FROM students WHERE score = 88;
AAYUSH EXPLAIN ANALYZE SELECT id FROM students WHERE score BETWEEN 70 AND 90;
```

`EXPLAIN` never executes the statement. `EXPLAIN ANALYZE` executes it and reports
measured row counts per operator. The optimizer may still choose a table scan when
it is cheaper; an index is an option, not a mandate. Indexes are single-column, and
`NULL` values are not indexed.

---

## 8. Expressions, CASE, DISTINCT, OFFSET

```sql
AAYUSH SELECT name, score, score + 5 AS boosted FROM students ORDER BY id;

AAYUSH SELECT name,
       CASE WHEN score IS NULL THEN 'unknown'
            WHEN score >= 90 THEN 'excellent'
            WHEN score >= 80 THEN 'good'
            ELSE 'review' END AS band
FROM students
ORDER BY id;

AAYUSH SELECT DISTINCT department_id FROM students ORDER BY department_id;

AAYUSH SELECT name FROM students ORDER BY score DESC, id LIMIT 2 OFFSET 1;
```

Arithmetic: `+ - * / %` (integer division rules: `/` is always float, `%` is
floored). `CASE` takes the first branch that is `TRUE`; `FALSE` and `UNKNOWN` never
match. `OFFSET` is applied after `ORDER BY` and before `LIMIT`.

---

## 9. Transactions

```sql
AAYUSH BEGIN;
AAYUSH UPDATE students SET score = 95 WHERE id = 1;
AAYUSH INSERT INTO students (id, name, department_id, score) VALUES (5, 'Barbara', 1, 81);
AAYUSH COMMIT;
```

```sql
AAYUSH BEGIN;
AAYUSH DELETE FROM students WHERE id = 5;
AAYUSH ROLLBACK;   -- id 5 is back
```

One transaction at a time. Uncommitted changes are visible to later statements in
the same session and are never visible to another engine or client. Closing an
engine with an open transaction rolls it back.

---

## 10. Persistence

Everything committed is durable in `university.aaydb` (plus a short-lived
`university.aaydb.wal` while a commit is in flight). Close the shell and reopen:

```bash
aaysql --db university.aaydb
```

```sql
AAYUSH SELECT COUNT(*) FROM students;
```

Use the same path from Python with `Engine("university.aaydb")`.

---

## 11. Client/server mode

Start a server on loopback (the default; no authentication needed):

```bash
aaysql-server --db university.aaydb --port 8765
```

Talk to it with the Python client:

```python
from aaysql.client import AayushSQLClient

with AayushSQLClient("127.0.0.1", 8765) as client:
    print(client.server_version)
    result = client.execute("AAYUSH SELECT name, score FROM students ORDER BY id")
    for row in result["rows"]:
        print(row)
```

The client speaks protocol v1 over a length-prefixed JSON frame. It never retries a
statement automatically: if the connection breaks after a request was sent, it
raises `ConnectionLostError`/`RequestTimeoutError` with `outcome_unknown = True`, and
you should reconnect and check the database before deciding what to do.

To listen beyond loopback you **must** configure both a token and TLS:

```bash
export AAYUSQL_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
aaysql-server --db university.aaydb --host 0.0.0.0 --port 8765 \
  --tls-cert server.crt --tls-key server.key
```

```python
client = AayushSQLClient(
    "db.example.com", 8765,
    token="…",
    tls=True,
    ca_certs="ca.crt",
    server_hostname="db.example.com",
)
```

The server refuses to start with a non-loopback host and no token or no TLS.

---

## 12. Backup and restore

Backup is **offline**: stop the server first.

```bash
aaysql-backup backup university.aaydb backups/university-2026-11-11
aaysql-backup verify backups/university-2026-11-11
```

Restore to a new path, or over the original with `--force`:

```bash
aaysql-backup restore backups/university-2026-11-11 university-restored.aaydb
```

A backup is a directory containing `database.aaydb` and `manifest.json` (format
versions, creation time, table row counts, index names, SHA-256). Restore verifies
the checksum, refuses to replace a database a server owns, refuses a target with
unrecovered WAL data, validates the restored copy before replacing anything, and
keeps the previous database at `<target>.pre-restore-<id>` until validation passes.

See [OPERATIONS.md](OPERATIONS.md) for recovery and troubleshooting.
