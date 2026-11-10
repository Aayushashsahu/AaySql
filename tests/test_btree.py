import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from aaysql.btree import BPlusTree, BTreeError


class Value:
    """Identity-distinct value so duplicate keys stay unambiguous."""

    __slots__ = ("seq",)

    def __init__(self, seq: int):
        self.seq = seq

    def __repr__(self) -> str:
        return f"Value({self.seq})"


def _pairs(tree: BPlusTree) -> list[tuple[int, int]]:
    return [(key, value.seq) for key, value in tree.items()]


def test_empty_tree():
    tree = BPlusTree()
    assert len(tree) == 0
    assert tree.is_empty()
    assert tree.items() == []
    assert tree.search(10) == []
    assert list(tree.scan()) == []
    assert tree.depth == 1
    tree.validate()


def test_invalid_capacities_are_rejected():
    with pytest.raises(ValueError):
        BPlusTree(leaf_capacity=2)
    with pytest.raises(ValueError):
        BPlusTree(leaf_capacity=4, internal_capacity=2)


def test_sequential_inserts_stay_ordered_and_split():
    tree = BPlusTree(leaf_capacity=4)
    for seq in range(200):
        tree.insert(seq, Value(seq))
        tree.validate()
    assert tree.depth > 1
    assert _pairs(tree) == [(seq, seq) for seq in range(200)]


def test_reverse_and_random_inserts_stay_ordered():
    for values in (list(range(150, -1, -1)), [random.Random(7).randint(0, 60) for _ in range(150)]):
        tree = BPlusTree(leaf_capacity=4)
        for seq, key in enumerate(values):
            tree.insert(key, Value(seq))
        tree.validate()
        assert [key for key, _ in tree.items()] == sorted(values)


def test_duplicate_keys_are_all_preserved_in_insertion_order():
    tree = BPlusTree(leaf_capacity=4)
    values = [Value(seq) for seq in range(60)]
    for value in values:
        tree.insert(7, value)
    tree.validate()
    assert tree.search(7) == values
    assert len(tree) == 60
    assert tree.search(8) == []


def test_duplicates_split_across_nodes_and_survive_deletes():
    tree = BPlusTree(leaf_capacity=3)
    values = [Value(seq) for seq in range(500)]
    for value in values:
        tree.insert(11, value)
    tree.validate()
    assert tree.depth > 2
    assert tree.search(11) == values

    for value in values[::2]:
        tree.delete(11, value)
    tree.validate()
    assert tree.search(11) == values[1::2]
    assert len(tree) == 250


def test_equality_and_range_lookups():
    tree = BPlusTree(leaf_capacity=4)
    for seq in range(0, 40, 2):
        tree.insert(seq, Value(seq))

    assert [value.seq for value in tree.search(20)] == [20]
    assert [value.seq for value in tree.search(21)] == []

    assert [k for k, _ in tree.scan(start=10, end=16)] == [10, 12, 14, 16]
    assert [k for k, _ in tree.scan(start=10, end=16, start_inclusive=False)] == [12, 14, 16]
    assert [k for k, _ in tree.scan(start=10, end=16, end_inclusive=False)] == [10, 12, 14]
    assert [k for k, _ in tree.scan(start=30)] == [30, 32, 34, 36, 38]
    assert [k for k, _ in tree.scan(end=4)] == [0, 2, 4]
    assert [k for k, _ in tree.scan(start=100)] == []
    assert [k for k, _ in tree.scan(end=-5)] == []


def test_text_keys_are_supported():
    tree = BPlusTree(leaf_capacity=4)
    for word in ["mika", "aayush", "alex", "zoe", "kabir"]:
        tree.insert(word, Value(len(word)))
    tree.validate()
    assert [key for key, _ in tree.items()] == ["aayush", "alex", "kabir", "mika", "zoe"]
    assert [k for k, _ in tree.scan(start="b", end="n")] == ["kabir", "mika"]


def test_delete_missing_entry_raises():
    tree = BPlusTree()
    tree.insert(1, Value(1))
    with pytest.raises(KeyError):
        tree.delete(1, Value(2))
    with pytest.raises(KeyError):
        tree.delete(99, Value(1))


def test_deleting_everything_returns_to_an_empty_leaf_root():
    tree = BPlusTree(leaf_capacity=4)
    values = [(seq, Value(seq)) for seq in range(120)]
    for key, value in values:
        tree.insert(key, value)
    tree.validate()

    for key, value in values:
        tree.delete(key, value)
        tree.validate()

    assert len(tree) == 0
    assert tree.items() == []
    assert tree.depth == 1


def test_bulk_delete_merges_and_redistributes_nodes():
    tree = BPlusTree(leaf_capacity=4)
    values = [(seq, Value(seq)) for seq in range(300)]
    for key, value in values:
        tree.insert(key, value)

    # Remove from the middle outward to exercise both borrowing and merging.
    order = sorted(values, key=lambda pair: abs(pair[0] - 150))
    remaining = dict(values)
    for key, value in order:
        tree.delete(key, value)
        del remaining[key]
        tree.validate()
        assert [k for k, _ in tree.items()] == sorted(remaining)
    assert len(tree) == 0


def test_randomized_operations_match_reference_ordering():
    rng = random.Random(20261110)
    tree = BPlusTree(leaf_capacity=4)
    model: list[tuple[int, Value]] = []
    counter = 0

    for step in range(2000):
        if model and rng.random() < 0.45:
            key, value = model[rng.randrange(len(model))]
            tree.delete(key, value)
            model.remove((key, value))
        else:
            key = rng.randrange(0, 26)
            value = Value(counter)
            counter += 1
            tree.insert(key, value)
            model.append((key, value))

        if step % 20 == 0:
            tree.validate()
            assert len(tree) == len(model)
            expected = sorted(model, key=lambda pair: (pair[0], pair[1].seq))
            assert _pairs(tree) == [(key, value.seq) for key, value in expected]

    tree.validate()
    for bound in (0, 5, 12, 25):
        expected = sorted(
            (key, value.seq) for key, value in model if key >= bound
        )
        actual = [(key, value.seq) for key, value in tree.scan(start=bound)]
        assert actual == sorted(expected, key=lambda pair: pair[0]) or actual == expected

    for low, high in ((3, 9), (0, 25), (10, 10)):
        expected = sorted(
            (key, value.seq) for key, value in model if low <= key <= high
        )
        actual = [(key, value.seq) for key, value in tree.scan(start=low, end=high)]
        assert actual == expected


def test_validation_detects_corrupted_separator_keys():
    tree = BPlusTree(leaf_capacity=3)
    for seq in range(40):
        tree.insert(seq, Value(seq))
    assert isinstance(tree.root, object)
    assert tree.depth > 1

    tree.root.keys[0] = (10_000, 0)
    with pytest.raises(BTreeError):
        tree.validate()
