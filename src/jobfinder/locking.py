"""Process-level run lock using flock(2), compatible with flock(1) in systemd units."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockHeldError(Exception):
    """Another process holds the lock."""


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on ``path`` for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeldError(str(path)) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
