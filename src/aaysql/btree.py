"""B+ tree used by AayushSQL secondary indexes.

Implemented from scratch: internal nodes route by separator keys, leaf nodes hold
sorted entries, and leaves are linked so ordered range scans never need to revisit
internal nodes.

Duplicate key support: physical entries are ``(key, sequence)`` pairs where the
sequence is a monotonically increasing insertion counter. Duplicate *key values*
therefore map to distinct physical keys, which keeps splits, merges, and lookups
uniform while the public API stays expressed in plain key values.
"""

from __future__ import annotations

from typing import Any, Iterator

# Sequence sentinels used to build exclusive/inclusive range bounds for a key.
_LOW = float("-inf")
_HIGH = float("inf")
_MISSING = object()


class BTreeError(Exception):
    """Raised when a B+ tree invariant is violated."""


class _Leaf:
    __slots__ = ("entries", "next")

    def __init__(self) -> None:
        self.entries: list[tuple[Any, Any]] = []
        self.next: _Leaf | None = None


class _Internal:
    __slots__ = ("keys", "children")

    def __init__(self) -> None:
        # keys[i] is the minimum key stored in children[i + 1].
        self.keys: list[Any] = []
        self.children: list[Any] = []


class BPlusTree:
    """Ordered multi-level B+ tree with duplicate keys and range traversal."""

    def __init__(self, leaf_capacity: int = 8, internal_capacity: int | None = None) -> None:
        if leaf_capacity < 3:
            raise ValueError("leaf_capacity must be at least 3")
        if internal_capacity is None:
            internal_capacity = leaf_capacity + 1
        if internal_capacity < 3:
            raise ValueError("internal_capacity must be at least 3")
        self.leaf_capacity = leaf_capacity
        self.internal_capacity = internal_capacity
        self.min_leaf = (leaf_capacity + 1) // 2
        self.min_children = (internal_capacity + 1) // 2
        self.root: _Leaf | _Internal = _Leaf()
        self._next_sequence = 0
        self._size = 0

    # --- introspection ---

    def __len__(self) -> int:
        return self._size

    def is_empty(self) -> bool:
        return self._size == 0

    @property
    def depth(self) -> int:
        """Number of levels in the tree (a leaf-only tree has depth 1)."""
        level = 1
        node = self.root
        while isinstance(node, _Internal):
            level += 1
            node = node.children[0]
        return level

    # --- mutation ---

    def insert(self, key: Any, value: Any) -> None:
        """Insert a key/value pair. Duplicate keys are allowed."""
        sequence = self._next_sequence
        self._next_sequence += 1
        composite = (key, sequence)

        path: list[_Internal] = []
        node = self.root
        while isinstance(node, _Internal):
            path.append(node)
            node = node.children[self._child_index(node, composite)]

        node.entries.insert(self._leaf_position(node, composite), (composite, value))
        self._size += 1
        if len(node.entries) > self.leaf_capacity:
            self._split_leaf(path, node)

    def delete(self, key: Any, value: Any) -> None:
        """Remove one entry for ``key`` whose value is ``value`` (identity match)."""
        node = self._leaf_for_bound((key, _LOW))
        target: Any | None = None
        while node is not None:
            for composite, entry_value in node.entries:
                if composite[0] == key and entry_value is value:
                    target = composite
                    break
            if target is not None:
                break
            if node.entries and node.entries[-1][0] > (key, _HIGH):
                break
            node = node.next
        if target is None:
            raise KeyError(f"No entry for key {key!r} holding the given value")
        self._remove_composite(target)

    # --- lookup ---

    def search(self, key: Any) -> list[Any]:
        """Return every value stored under ``key``, in insertion order."""
        return [value for _, value in self.scan(start=key, end=key)]

    def scan(
        self,
        start: Any = _MISSING,
        end: Any = _MISSING,
        start_inclusive: bool = True,
        end_inclusive: bool = True,
    ) -> Iterator[tuple[Any, Any]]:
        """Yield ``(key, value)`` pairs in key order, bounded on either side."""
        lo = None
        if start is not _MISSING:
            lo = (start, _LOW if start_inclusive else _HIGH)
        hi = None
        if end is not _MISSING:
            hi = (end, _HIGH if end_inclusive else _LOW)
        for composite, value in self._iter_composites(lo, hi):
            yield composite[0], value

    def items(self) -> list[tuple[Any, Any]]:
        """Every ``(key, value)`` pair in key order."""
        return list(self.scan())

    # --- invariant validation ---

    def validate(self) -> None:
        """Verify structural invariants; raises ``BTreeError`` on corruption."""
        state = _ValidationState(self)
        self._validate_node(self.root, is_root=True, depth=0, state=state)
        if len(state.leaf_depths) > 1:
            raise BTreeError(f"Leaves are not at a uniform depth: {sorted(state.leaf_depths)}")

        count = 0
        previous: Any | None = None
        for index, leaf in enumerate(state.leaves):
            for composite, _ in leaf.entries:
                if previous is not None and not previous < composite:
                    raise BTreeError("Tree entries are not globally ordered")
                previous = composite
                count += 1
            expected_next = state.leaves[index + 1] if index + 1 < len(state.leaves) else None
            if leaf.next is not expected_next:
                raise BTreeError("Leaf links do not match the ordered leaf chain")
        if count != self._size:
            raise BTreeError(f"Size counter mismatch: stored {self._size}, counted {count}")

    # --- internals: search helpers ---

    @staticmethod
    def _child_index(node: _Internal, composite: Any) -> int:
        index = 0
        while index < len(node.keys) and node.keys[index] <= composite:
            index += 1
        return index

    @staticmethod
    def _leaf_position(node: _Leaf, composite: Any) -> int:
        low, high = 0, len(node.entries)
        while low < high:
            mid = (low + high) // 2
            if node.entries[mid][0] <= composite:
                low = mid + 1
            else:
                high = mid
        return low

    def _leaf_for_bound(self, bound: Any) -> _Leaf:
        """The leftmost leaf that can contain ``bound`` (or the leftmost leaf)."""
        node = self.root
        while isinstance(node, _Internal):
            node = node.children[self._child_index(node, bound) if bound is not None else 0]
        return node

    def _iter_composites(self, lo: Any, hi: Any) -> Iterator[tuple[Any, Any]]:
        node = self._leaf_for_bound(lo)
        while node is not None:
            for composite, value in node.entries:
                if lo is not None and composite < lo:
                    continue
                if hi is not None and composite > hi:
                    return
                yield composite, value
            node = node.next

    @staticmethod
    def _count(node: Any) -> int:
        return len(node.entries) if isinstance(node, _Leaf) else len(node.children)

    @staticmethod
    def _min_count(node: Any, tree: "BPlusTree") -> int:
        return tree.min_leaf if isinstance(node, _Leaf) else tree.min_children

    def _min_key(self, node: Any) -> Any:
        while isinstance(node, _Internal):
            node = node.children[0]
        return node.entries[0][0]

    @staticmethod
    def _child_index_of(parent: _Internal, child: Any) -> int | None:
        for index, candidate in enumerate(parent.children):
            if candidate is child:
                return index
        return None

    def _refresh_separator(self, parent: _Internal, index: int) -> None:
        if 0 < index < len(parent.children):
            parent.keys[index - 1] = self._min_key(parent.children[index])

    # --- internals: insertion ---

    def _split_leaf(self, path: list[_Internal], node: _Leaf) -> None:
        mid = len(node.entries) // 2
        right = _Leaf()
        right.entries = node.entries[mid:]
        node.entries = node.entries[:mid]
        right.next = node.next
        node.next = right
        self._insert_into_parent(path, node, right.entries[0][0], right)

    def _split_internal(self, path: list[_Internal], node: _Internal) -> None:
        mid = len(node.children) // 2
        right = _Internal()
        right.children = node.children[mid:]
        node.children = node.children[:mid]
        promoted = node.keys[mid - 1]
        right.keys = node.keys[mid:]
        node.keys = node.keys[: mid - 1]
        self._insert_into_parent(path, node, promoted, right)

    def _insert_into_parent(
        self, path: list[_Internal], left: Any, separator: Any, right: Any
    ) -> None:
        if not path:
            root = _Internal()
            root.keys = [separator]
            root.children = [left, right]
            self.root = root
            return
        parent = path.pop()
        index = self._child_index_of(parent, left)
        if index is None:
            raise BTreeError("Split child is missing from its parent")
        parent.keys.insert(index, separator)
        parent.children.insert(index + 1, right)
        if len(parent.children) > self.internal_capacity:
            self._split_internal(path, parent)

    # --- internals: deletion ---

    def _remove_composite(self, composite: Any) -> None:
        path: list[_Internal] = []
        node = self.root
        while isinstance(node, _Internal):
            path.append(node)
            node = node.children[self._child_index(node, composite)]

        index = None
        for position, (candidate, _) in enumerate(node.entries):
            if candidate == composite:
                index = position
                break
        if index is None:
            raise BTreeError("Composite key is missing from its leaf")

        del node.entries[index]
        self._size -= 1
        self._fix_after_delete(path, node)

    def _fix_after_delete(self, path: list[_Internal], node: Any) -> None:
        child = node
        while path:
            parent = path.pop()
            index = self._child_index_of(parent, child)
            if index is None:
                raise BTreeError("Lost child while repairing deletion")
            self._refresh_separator(parent, index)
            if self._count(child) < self._min_count(child, self):
                self._rebalance(parent, index)
            child = parent
        while isinstance(self.root, _Internal) and len(self.root.children) == 1:
            self.root = self.root.children[0]

    def _rebalance(self, parent: _Internal, index: int) -> None:
        node = parent.children[index]
        left = parent.children[index - 1] if index > 0 else None
        right = parent.children[index + 1] if index + 1 < len(parent.children) else None

        if left is not None and self._count(left) > self._min_count(left, self):
            if isinstance(node, _Leaf):
                node.entries.insert(0, left.entries.pop())
            else:
                self._borrow_internal_from_left(parent, index)
            self._refresh_separator(parent, index)
            if isinstance(node, _Leaf):
                self._refresh_separator(parent, index - 1)
            return

        if right is not None and self._count(right) > self._min_count(right, self):
            if isinstance(node, _Leaf):
                node.entries.append(right.entries.pop(0))
            else:
                self._borrow_internal_from_right(parent, index)
            self._refresh_separator(parent, index + 1)
            return

        merged_index = index - 1 if left is not None else index
        self._merge(parent, merged_index)
        self._refresh_separator(parent, merged_index)

    def _borrow_internal_from_left(self, parent: _Internal, index: int) -> None:
        left = parent.children[index - 1]
        node = parent.children[index]
        moved = left.children.pop()
        separator = left.keys.pop()
        node.keys.insert(0, parent.keys[index - 1])
        node.children.insert(0, moved)
        parent.keys[index - 1] = separator

    def _borrow_internal_from_right(self, parent: _Internal, index: int) -> None:
        right = parent.children[index + 1]
        node = parent.children[index]
        moved = right.children.pop(0)
        separator = right.keys.pop(0)
        node.children.append(moved)
        node.keys.append(parent.keys[index])
        parent.keys[index] = separator

    def _merge(self, parent: _Internal, index: int) -> None:
        left = parent.children[index]
        right = parent.children[index + 1]
        if isinstance(left, _Leaf):
            left.entries.extend(right.entries)
            left.next = right.next
        else:
            left.keys.append(parent.keys[index])
            left.keys.extend(right.keys)
            left.children.extend(right.children)
        del parent.keys[index]
        del parent.children[index + 1]

    # --- internals: validation ---

    def _validate_node(self, node: Any, *, is_root: bool, depth: int, state: "_ValidationState") -> None:
        if isinstance(node, _Leaf):
            for position in range(1, len(node.entries)):
                if not node.entries[position - 1][0] < node.entries[position][0]:
                    raise BTreeError("Leaf entries are not strictly increasing")
            if len(node.entries) > self.leaf_capacity:
                raise BTreeError("Leaf exceeds capacity")
            if not is_root and len(node.entries) < self.min_leaf:
                raise BTreeError("Leaf is below minimum occupancy")
            state.leaf_depths.add(depth)
            state.leaves.append(node)
            return

        if len(node.children) != len(node.keys) + 1:
            raise BTreeError("Internal node key/child counts are inconsistent")
        if not node.keys:
            raise BTreeError("Internal node has no separator keys")
        for position in range(1, len(node.keys)):
            if not node.keys[position - 1] < node.keys[position]:
                raise BTreeError("Internal separator keys are not strictly increasing")
        if len(node.children) > self.internal_capacity:
            raise BTreeError("Internal node exceeds capacity")
        if not is_root and len(node.children) < self.min_children:
            raise BTreeError("Internal node is below minimum occupancy")
        if is_root and len(node.children) < 2:
            raise BTreeError("Root internal node must have at least two children")

        for position, child in enumerate(node.children):
            if position > 0 and self._min_key(child) != node.keys[position - 1]:
                raise BTreeError("Separator key does not match child minimum")
            self._validate_node(child, is_root=False, depth=depth + 1, state=state)


class _ValidationState:
    __slots__ = ("leaf_depths", "leaves")

    def __init__(self, _tree: BPlusTree) -> None:
        self.leaf_depths: set[int] = set()
        self.leaves: list[_Leaf] = []
