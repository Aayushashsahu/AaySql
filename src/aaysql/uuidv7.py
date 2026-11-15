"""RFC 9562 compliant, dependency-free UUIDv7 generator for Python 3.10+.

Layout (128 bits):
- 48 bits: Unix timestamp in milliseconds.
- 4 bits: version 7 (0b0111).
- 12 bits: sub-millisecond sequence counter ensuring monotonic ordering.
- 2 bits: variant 1 (0b10).
- 62 bits: cryptographically secure pseudorandom bits.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_LAST_V7_MS = -1
_V7_SEQ = 0


def generate_uuidv7() -> str:
    """Generate a new monotonic UUIDv7 string."""
    global _LAST_V7_MS, _V7_SEQ

    with _LOCK:
        now_ms = time.time_ns() // 1_000_000
        if now_ms <= _LAST_V7_MS:
            now_ms = _LAST_V7_MS
            _V7_SEQ = (_V7_SEQ + 1) & 0xFFF
            if _V7_SEQ == 0:
                # Sequence overflow within same millisecond: advance virtual millisecond
                now_ms += 1
                _LAST_V7_MS = now_ms
        else:
            _LAST_V7_MS = now_ms
            _V7_SEQ = int.from_bytes(os.urandom(2), "big") & 0xFFF

        time_high = (now_ms >> 16) & 0xFFFFFFFF
        time_mid = now_ms & 0xFFFF
        time_hi_and_version = 0x7000 | _V7_SEQ

        rand_bytes = os.urandom(8)
        rand_a = int.from_bytes(rand_bytes[:2], "big")
        clk_seq_and_variant = 0x8000 | (rand_a & 0x3FFF)
        rand_b = int.from_bytes(rand_bytes[2:], "big") & 0xFFFFFFFFFFFF

        return f"{time_high:08x}-{time_mid:04x}-{time_hi_and_version:04x}-{clk_seq_and_variant:04x}-{rand_b:012x}"


def is_uuidv7(val: Any) -> bool:
    """Return True if val is a valid formatted UUIDv7 string."""
    if not isinstance(val, str) or len(val) != 36:
        return False
    # Format: 8-4-4-4-12
    if val[8] != "-" or val[13] != "-" or val[18] != "-" or val[23] != "-":
        return False
    if val[14] != "7":
        return False
    if val[19] not in ("8", "9", "a", "b", "A", "B"):
        return False
    try:
        int(val.replace("-", ""), 16)
        return True
    except ValueError:
        return False
