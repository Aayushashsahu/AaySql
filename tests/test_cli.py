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
