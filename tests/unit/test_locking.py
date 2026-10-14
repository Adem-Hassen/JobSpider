from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobfinder.locking import LockHeldError, exclusive_lock


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with exclusive_lock(path), pytest.raises(LockHeldError), exclusive_lock(path):
        pass
    with exclusive_lock(path):
        pass


def test_compatible_with_flock_command(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with exclusive_lock(path):
        result = subprocess.run(  # noqa: S603
            ["flock", "--nonblock", str(path), "true"],  # noqa: S607
            check=False,
        )
    assert result.returncode == 1
