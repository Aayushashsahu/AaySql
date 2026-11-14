from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .engine import Engine
from .errors import AayushSQLError
from .formatting import format_result

DEFAULT_DB_PATH = "aaysql.aaydb"

BANNER = rf"""
   _    _                    _     _   _____  _     
  / \  | |  _   _  ___  ___| |__ | | / ____|| |
 / _ \ | | | | | |/ __|/ __| '_ \| || (___  | |
/ ___ \| | | |_| | (__| (__| | | | | \___ \ | |
/_/   \_\_|  \__,_|\___|\___|_| |_|_|_____)|_|

AayushSQL v{__version__} — dumb idea, serious implementation.
Every statement must begin with AAYUSH.
"""


def _strip_comments_and_ws(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    out.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                i += 1
        else:
            if not ch.isspace():
                out.append(ch)
            i += 1
    return "".join(out)


def split_statements(text: str) -> list[str]:
    """Split SQL text on semicolons outside string literals and line comments."""
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    in_comment = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if in_comment:
            current.append(ch)
            if ch == "\n":
                in_comment = False
            i += 1
        elif quote is not None:
            current.append(ch)
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    current.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            in_comment = True
            current.append("--")
            i += 2
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
        elif ch == ";":
            chunk = "".join(current)
            if _strip_comments_and_ws(chunk):
                statements.append(chunk + ";")
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1

    trailing = "".join(current)
    if _strip_comments_and_ws(trailing):
        statements.append(trailing)
    return statements


def _has_complete_statement(text: str) -> bool:
    quote: str | None = None
    in_comment = False
    i = 0
    n = len(text)
    saw_semicolon = False
    while i < n:
        ch = text[i]
        if in_comment:
            if ch == "\n":
                in_comment = False
            i += 1
        elif quote is not None:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            in_comment = True
            i += 2
        elif ch in ("'", '"'):
            quote = ch
            i += 1
        elif ch == ";":
            saw_semicolon = True
            i += 1
        else:
            i += 1
    return saw_semicolon and quote is None


def run_buffer(engine: Engine, text: str, *, stop_on_error: bool = False) -> bool:
    """Execute statements in text. Returns True if all statements succeeded."""
    for stmt in split_statements(text):
        try:
            print(format_result(engine.execute(stmt)))
        except AayushSQLError as exc:
            err_stream = sys.stderr if stop_on_error else sys.stdout
            print(f"ERROR: {exc}", file=err_stream)
            if stop_on_error:
                return False
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aaysql",
        description="AayushSQL — relational database engine requiring the AAYUSH prefix.",
    )
    storage_group = parser.add_mutually_exclusive_group()
    storage_group.add_argument(
        "--db",
        metavar="PATH",
        help=f"Path to persistent database file (default: {DEFAULT_DB_PATH}, or ':memory:' for in-memory).",
    )
    storage_group.add_argument(
        "--memory",
        action="store_true",
        help="Run in temporary in-memory mode without writing to disk.",
    )
    parser.add_argument(
        "--migrate",
        metavar="PATH",
        help="Convert a format version 1-3 database to format v4 and exit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"aaysql {__version__}",
    )
    parser.add_argument(
        "script",
        nargs="?",
        help="Optional path to an AayushSQL (.aay) script file to execute.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.migrate:
        if args.memory or args.db == ":memory:":
            print("ERROR: --migrate requires a persistent database path", file=sys.stderr)
            return 1
        from .storage import migrate_to_v4

        try:
            report = migrate_to_v4(args.migrate)
        except AayushSQLError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(report["message"])
        return 0

    if args.memory or args.db == ":memory:":
        db_path: str | None = None
    elif args.db is not None:
        db_path = args.db
    else:
        db_path = DEFAULT_DB_PATH

    try:
        engine = Engine(db_path=db_path)
    except AayushSQLError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(BANNER)
    if engine.is_persistent:
        print(f"Database: {engine.db_path} (persistent)")
    else:
        print("Database: :memory: (in-memory)")

    try:
        if args.script:
            path = Path(args.script)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"ERROR: Cannot read script '{path}': {exc}", file=sys.stderr)
                return 1
            return 0 if run_buffer(engine, text, stop_on_error=True) else 1

        buffer: list[str] = []
        while True:
            try:
                prompt = "aaysql> " if not buffer else "      -> "
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not buffer and line.strip().lower() in {"exit", "quit", ".exit"}:
                break
            buffer.append(line)
            joined = "\n".join(buffer)
            if _has_complete_statement(joined):
                buffer.clear()
                run_buffer(engine, joined)
        return 0
    finally:
        if engine.in_transaction:
            print(
                "WARNING: rolling back uncommitted transaction on exit",
                file=sys.stderr,
            )
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
