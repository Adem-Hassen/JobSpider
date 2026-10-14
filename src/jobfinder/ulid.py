"""Minimal ULID generation (https://github.com/ulid/spec): 48-bit ms time + 80 random bits."""

from __future__ import annotations

import secrets
from datetime import datetime

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_MAX_MS = (1 << 48) - 1


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, index = divmod(value, 32)
        chars.append(_ALPHABET[index])
    return "".join(reversed(chars))


def new_ulid(at: datetime) -> str:
    """A 26-character ULID for timezone-aware ``at``; sorts by time across milliseconds."""
    if at.tzinfo is None:
        raise ValueError("ULID timestamp must be timezone-aware")
    ms = int(at.timestamp() * 1000)
    if not 0 <= ms <= _MAX_MS:
        raise ValueError("timestamp out of ULID range")
    return _encode(ms, 10) + _encode(secrets.randbits(80), 16)


def ulid_timestamp_ms(value: str) -> int:
    if len(value) != 26 or any(c not in _ALPHABET for c in value):
        raise ValueError(f"not a ULID: {value!r}")
    ms = 0
    for char in value[:10]:
        ms = ms * 32 + _ALPHABET.index(char)
    return ms
