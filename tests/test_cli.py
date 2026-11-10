import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.cli import main


def _run_cli(args: list[str], cwd: Path, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    src_dir = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "aaysql.cli", *args],
        cwd=cwd,
        input=stdin_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_cli_persistent_two_sessions(tmp_path: Path):
    db_file = tmp_path / "university.aaydb"

    session1 = """
AAYUSH CREATE TABLE users (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    age INT
);
AAYUSH INSERT INTO users VALUES (1, 'Aayush', 19);
AAYUSH INSERT INTO users VALUES (2, 'Alex', 20);
exit
"""
    p1 = _run_cli(["--db", str(db_file)], cwd=tmp_path, stdin_text=session1)
    assert p1.returncode == 0, p1.stderr
    assert db_file.exists()

    session2 = "AAYUSH SELECT * FROM users ORDER BY id ASC;\nexit\n"
    p2 = _run_cli(["--db", str(db_file)], cwd=tmp_path, stdin_text=session2)
    assert p2.returncode == 0, p2.stderr
    assert "| id | name   | age |" in p2.stdout
    assert "| 1  | Aayush | 19  |" in p2.stdout
    assert "| 2  | Alex   | 20  |" in p2.stdout
    assert "(2 rows)" in p2.stdout


def test_cli_memory_mode_does_not_create_file(tmp_path: Path):
    p = _run_cli(
        ["--memory"],
        cwd=tmp_path,
        stdin_text="AAYUSH CREATE TABLE t (id INT PRIMARY KEY);\nAAYUSH INSERT INTO t VALUES (1);\nAAYUSH SELECT * FROM t;\nexit\n",
    )
    assert p.returncode == 0, p.stderr
    assert "Database: :memory: (in-memory)" in p.stdout
    assert "(1 row)" in p.stdout
    assert list(tmp_path.iterdir()) == []


def test_cli_db_colon_memory_mode(tmp_path: Path):
    p = _run_cli(
        ["--db", ":memory:"],
        cwd=tmp_path,
        stdin_text="AAYUSH CREATE TABLE t (id INT PRIMARY KEY);\nexit\n",
    )
    assert p.returncode == 0
    assert "Database: :memory: (in-memory)" in p.stdout
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_malformed_database_file(tmp_path: Path):
    bad_db = tmp_path / "corrupt.aaydb"
    bad_db.write_text("{corrupt", encoding="utf-8")
    p = _run_cli(["--db", str(bad_db)], cwd=tmp_path, stdin_text="exit\n")
    assert p.returncode == 1
    assert "ERROR:" in p.stderr
    assert bad_db.read_text(encoding="utf-8") == "{corrupt"


def test_cli_rejects_conflicting_db_and_memory_flags():
    import pytest
    with pytest.raises(SystemExit) as exc:
        main(["--db", "foo.aaydb", "--memory"])
    assert exc.value.code == 2


def test_cli_demo_script_in_memory(tmp_path: Path):
    demo_path = Path(__file__).parents[1] / "examples" / "demo.aay"
    p = _run_cli(["--memory", str(demo_path)], cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "Table 'users' created" in p.stdout
    assert "| 1  | Aayush | 20  | Pune |" in p.stdout
    assert "ERROR:" not in p.stdout


def test_cli_version_flag(tmp_path: Path):
    p = _run_cli(["--version"], cwd=tmp_path)
    assert p.returncode == 0
    assert "aaysql 0.3.0" in p.stdout


def test_cli_script_success_returns_zero(tmp_path: Path):
    script = tmp_path / "ok.aay"
    db_file = tmp_path / "ok.aaydb"
    script.write_text(
        "AAYUSH CREATE TABLE users (id INT PRIMARY KEY);\n"
        "AAYUSH INSERT INTO users VALUES (1);\n",
        encoding="utf-8",
    )
    p = _run_cli(["--db", str(db_file), str(script)], cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "Table 'users' created" in p.stdout


def test_cli_script_missing_prefix_returns_nonzero(tmp_path: Path):
    script = tmp_path / "missing_prefix.aay"
    script.write_text("SELECT * FROM users;\n", encoding="utf-8")
    p = _run_cli(["--memory", str(script)], cwd=tmp_path)
    assert p.returncode != 0
    assert "ERROR:" in p.stderr
    assert "AAYUSH" in p.stderr


def test_cli_script_parser_failure_returns_nonzero(tmp_path: Path):
    script = tmp_path / "bad_update.aay"
    script.write_text(
        "AAYUSH CREATE TABLE users (id INT PRIMARY KEY, age INT);\n"
        "AAYUSH UPDATE users SET age 20 WHERE id = 1;\n",
        encoding="utf-8",
    )
    p = _run_cli(["--memory", str(script)], cwd=tmp_path)
    assert p.returncode != 0
    assert "ERROR:" in p.stderr


def test_cli_script_execution_failure_returns_nonzero(tmp_path: Path):
    script = tmp_path / "exec_err.aay"
    script.write_text(
        "AAYUSH CREATE TABLE users (id INT PRIMARY KEY);\n"
        "AAYUSH INSERT INTO users VALUES (1);\n"
        "AAYUSH INSERT INTO users VALUES (1);\n",
        encoding="utf-8",
    )
    p = _run_cli(["--memory", str(script)], cwd=tmp_path)
    assert p.returncode != 0
    assert "ERROR:" in p.stderr
    assert "PRIMARY KEY" in p.stderr


def test_cli_script_persistence_failure_returns_nonzero(tmp_path: Path):
    script = tmp_path / "ok.aay"
    script.write_text("AAYUSH CREATE TABLE t (id INT PRIMARY KEY);\n", encoding="utf-8")
    # Passing a directory as --db fails persistence on startup.
    p = _run_cli(["--db", str(tmp_path), str(script)], cwd=tmp_path)
    assert p.returncode != 0
    assert "ERROR:" in p.stderr


def test_cli_script_fail_fast_preserves_prior_and_stops_subsequent(tmp_path: Path):
    db_file = tmp_path / "failfast.aaydb"
    script = tmp_path / "mixed.aay"
    script.write_text(
        "AAYUSH CREATE TABLE users (id INT PRIMARY KEY, name TEXT);\n"
        "AAYUSH INSERT INTO users VALUES (1, 'Aayush');\n"
        "INSERT INTO users VALUES (2, 'BadPrefix');\n"
        "AAYUSH INSERT INTO users VALUES (3, 'ShouldNotRun');\n",
        encoding="utf-8",
    )
    p = _run_cli(["--db", str(db_file), str(script)], cwd=tmp_path)
    assert p.returncode != 0
    assert "ERROR:" in p.stderr

    verify = _run_cli(
        ["--db", str(db_file)],
        cwd=tmp_path,
        stdin_text="AAYUSH SELECT * FROM users;\nexit\n",
    )
    assert verify.returncode == 0
    assert "| 1  | Aayush |" in verify.stdout
    assert "ShouldNotRun" not in verify.stdout
    assert "(1 row)" in verify.stdout


def test_cli_repl_remains_usable_after_query_error(tmp_path: Path):
    repl_input = (
        "AAYUSH CREATE TABLE users (id INT PRIMARY KEY, age INT);\n"
        "SELECT * FROM users;\n"
        "AAYUSH UPDATE users SET age 20 WHERE id = 1;\n"
        "AAYUSH INSERT INTO users VALUES (1, 19);\n"
        "AAYUSH SELECT * FROM users WHERE age > 'abc';\n"
        "AAYUSH SELECT * FROM users;\n"
        "exit\n"
    )
    p = _run_cli(["--memory"], cwd=tmp_path, stdin_text=repl_input)
    assert p.returncode == 0
    assert "ERROR:" in p.stdout
    assert "| 1  | 19  |" in p.stdout
    assert "(1 row)" in p.stdout

