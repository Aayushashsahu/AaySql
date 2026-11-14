"""Unit tests for the in-memory undo journal and its inverse entries."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from aaysql.ast import ColumnDef
from aaysql.storage import Database, Table
from aaysql.undo import (
    CreateIndexUndo,
    CreateTableUndo,
    DeleteUndo,
    DropIndexUndo,
    DropTableUndo,
    InsertUndo,
    UndoError,
    UndoJournal,
    UpdateUndo,
)

COLUMNS = [
    ColumnDef("id", "INT", primary_key=True),
    ColumnDef("name", "TEXT"),
    ColumnDef("value", "INT"),
]


def _db(rows: list[tuple[int, str, int]] | None = None, *, index: bool = False) -> Database:
    db = Database()
    db.tables["t"] = Table("t", list(COLUMNS))
    db.tables["t"].rows = [
        {"id": row_id, "name": name, "value": value} for row_id, name, value in rows or []
    ]
    if index:
        db.create_index("idx_t_value", "t", "value")
    return db


def _values(db: Database) -> list[tuple[int, str, int]]:
    return [(row["id"], row["name"], row["value"]) for row in db.table("t").rows]


# --- journal mechanics -----------------------------------------------------


def test_empty_journal_and_marker_semantics():
    journal = UndoJournal()
    assert len(journal) == 0
    marker = journal.mark()
    assert marker == 0
    journal.rollback_to(marker, _db())
    journal.discard_to(marker)
    assert len(journal) == 0


def test_invalid_markers_are_rejected():
    journal = UndoJournal()
    db = _db()
    for marker in (-1, 1):
        with pytest.raises(ValueError):
            journal.rollback_to(marker, db)
        with pytest.raises(ValueError):
            journal.discard_to(marker)
    for marker in (None, True, "0"):
        with pytest.raises(TypeError):
            journal.rollback_to(marker, db)
        with pytest.raises(TypeError):
            journal.discard_to(marker)


def test_nested_marks_and_reverse_order_replay():
    db = _db()
    journal = UndoJournal()
    outer = journal.mark()
    journal.record(CreateTableUndo("a"))
    db.create_table("a", list(COLUMNS))
    inner = journal.mark()
    journal.record(CreateTableUndo("b"))
    db.create_table("b", list(COLUMNS))
    journal.rollback_to(inner, db)
    assert "a" in db.tables
    assert "b" not in db.tables
    assert len(journal) == inner
    journal.rollback_to(outer, db)
    assert "a" not in db.tables
    assert len(journal) == 0
    # Rolling back an already-applied marker is a no-op.
    journal.rollback_to(0, db)
    assert len(journal) == 0


def test_discard_to_keeps_earlier_entries():
    db = _db()
    journal = UndoJournal()
    journal.record(CreateTableUndo("a"))
    db.create_table("a", list(COLUMNS))
    marker = journal.mark()
    journal.record(CreateTableUndo("b"))
    db.create_table("b", list(COLUMNS))
    journal.discard_to(marker)
    assert len(journal) == 1
    assert "b" in db.tables
    journal.rollback_to(0, db)
    assert "a" not in db.tables
    assert "b" in db.tables


def test_recording_during_rollback_is_rejected():
    journal = UndoJournal()
    db = _db()

    class Reentrant:
        def apply(self, target):
            target.tables["x"] = Table("x", list(COLUMNS))
            journal.record(CreateTableUndo("x"))

    journal.record(Reentrant())
    with pytest.raises(RuntimeError, match="while rolling back"):
        journal.rollback_to(0, db)


def test_undo_entry_failure_propagates_and_keeps_entry():
    journal = UndoJournal()
    db = _db()

    class Broken:
        def apply(self, target):
            raise UndoError("cannot undo")

    journal.record(Broken())
    with pytest.raises(UndoError, match="cannot undo"):
        journal.rollback_to(0, db)
    assert len(journal) == 1


# --- row inverses ----------------------------------------------------------


def test_insert_undo_removes_exact_appended_rows_with_duplicates():
    db = _db([(1, "a", 10), (2, "b", 20)], index=True)
    journal = UndoJournal()
    table = db.table("t")
    appended = [
        {"id": 3, "name": "dup", "value": 5},
        {"id": 4, "name": "dup", "value": 5},
    ]
    journal.record(InsertUndo("t", len(table.rows), appended))
    table.rows.extend(appended)
    table.touch()
    for row in appended:
        db.indexes["idx_t_value"].add(row)
    assert len(db.indexes["idx_t_value"]) == 4
    journal.rollback_to(0, db)
    assert _values(db) == [(1, "a", 10), (2, "b", 20)]
    assert len(db.indexes["idx_t_value"]) == 2


def test_update_undo_restores_before_image_in_place_and_indexes():
    db = _db([(1, "a", 10), (2, "b", 20)], index=True)
    journal = UndoJournal()
    table = db.table("t")
    row = table.rows[0]
    journal.record(UpdateUndo("t", 0, dict(row)))
    index = db.indexes["idx_t_value"]
    index.remove(row, key=10, forget=False)
    row["value"] = 99
    row["name"] = "changed"
    index.add(row)
    table.touch()
    assert index.lookup(99) == [row]
    journal.rollback_to(0, db)
    assert _values(db)[0] == (1, "a", 10)
    assert index.lookup(10) == [table.rows[0]]
    assert index.lookup(99) == []
    assert table.rows[0] is row


def test_delete_undo_restores_positions_duplicates_and_index_order():
    db = _db(
        [(1, "a", 1), (2, "dup", 2), (3, "c", 1), (4, "dup", 2), (5, "e", 3)],
        index=True,
    )
    journal = UndoJournal()
    table = db.table("t")
    index = db.indexes["idx_t_value"]
    entries = [(3, table.rows[3]), (1, table.rows[1])]
    journal.record(DeleteUndo("t", entries))
    for row in (table.rows[3], table.rows[1]):
        index.remove(row)
    table.rows[:] = [
        row for row in table.rows if row not in (table.rows[1], table.rows[3])
    ]
    table.touch()
    journal.rollback_to(0, db)
    assert _values(db) == [
        (1, "a", 1),
        (2, "dup", 2),
        (3, "c", 1),
        (4, "dup", 2),
        (5, "e", 3),
    ]
    # Index scans must agree with table order after restoration.
    assert [row["id"] for row in index.lookup(2)] == [2, 4]
    assert [row["id"] for row in index.lookup(1)] == [1, 3]
    index.validate()


def test_delete_undo_rejects_impossible_positions():
    db = _db([(1, "a", 1)])
    journal = UndoJournal()
    journal.record(DeleteUndo("t", [(5, {"id": 9, "name": "x", "value": 9})]))
    with pytest.raises(UndoError, match="out of range"):
        journal.rollback_to(0, db)


def test_operating_on_a_dropped_table_fails_closed():
    db = _db([(1, "a", 1)])
    journal = UndoJournal()
    journal.record(InsertUndo("gone", 0, [{"id": 2, "name": "b", "value": 2}]))
    with pytest.raises(UndoError, match="unknown table"):
        journal.rollback_to(0, db)


# --- DDL inverses ----------------------------------------------------------


def test_create_table_undo_removes_table_and_statistics():
    db = _db()
    journal = UndoJournal()
    journal.record(CreateTableUndo("new"))
    db.create_table("new", list(COLUMNS))
    db.stats.get(db.table("new"))
    assert len(db.stats) == 1
    journal.rollback_to(0, db)
    assert "new" not in db.tables
    assert len(db.stats) == 0


def test_drop_table_undo_restores_rows_columns_and_indexes():
    db = _db([(1, "a", 10), (2, "b", 20)], index=True)
    db.create_index("idx_t_name", "t", "name")
    journal = UndoJournal()
    table = db.table("t")
    indexes = db.indexes_on("t")
    journal.record(DropTableUndo("t", table, indexes))
    db.drop_table("t")
    assert "t" not in db.tables
    assert not db.indexes
    journal.rollback_to(0, db)
    assert _values(db) == [(1, "a", 10), (2, "b", 20)]
    assert set(db.indexes) == {"idx_t_value", "idx_t_name"}
    assert db.indexes["idx_t_value"].lookup(20)[0]["id"] == 2
    assert db.indexes["idx_t_name"].lookup("a")[0]["id"] == 1


def test_create_index_undo_and_drop_index_undo_roundtrip():
    db = _db([(1, "a", 10)], index=True)
    journal = UndoJournal()
    original = db.indexes["idx_t_value"]
    journal.record(DropIndexUndo("idx_t_value", original))
    db.drop_index("idx_t_value")
    assert "idx_t_value" not in db.indexes
    journal.record(CreateIndexUndo("idx_t_name"))
    db.create_index("idx_t_name", "t", "name")
    journal.rollback_to(0, db)
    assert set(db.indexes) == {"idx_t_value"}
    assert db.indexes["idx_t_value"] is original
    journal2 = UndoJournal()
    journal2.record(CreateIndexUndo("idx_t_name"))
    db.create_index("idx_t_name", "t", "name")
    journal2.rollback_to(0, db)
    assert "idx_t_name" not in db.indexes


def test_ddl_undo_duplicate_names_fail_closed():
    db = _db()
    db.create_table("x", list(COLUMNS))
    journal = UndoJournal()
    journal.record(CreateTableUndo("missing"))
    with pytest.raises(UndoError, match="missing"):
        journal.rollback_to(0, db)
    journal.clear()
    journal.record(DropTableUndo("x", db.table("x")))
    with pytest.raises(UndoError, match="already exists"):
        journal.rollback_to(0, db)
    journal.clear()
    journal.record(CreateIndexUndo("nope"))
    with pytest.raises(UndoError, match="nope"):
        journal.rollback_to(0, db)
    journal.clear()
    db.create_index("idx", "x", "id")
    journal.record(DropIndexUndo("idx", db.indexes["idx"]))
    with pytest.raises(UndoError, match="already exists"):
        journal.rollback_to(0, db)


def test_index_helpers_are_idempotent():
    db = _db([(1, "a", 10)], index=True)
    index = db.indexes["idx_t_value"]
    row = db.table("t").rows[0]
    index.ensure(row)
    assert len(index) == 1
    index.discard(row)
    index.discard(row)
    assert len(index) == 0
    index.restore_positions([(0, row)])
    assert index.lookup(10) == [row]
