from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder.storage.atomic import atomic_write_bytes


def test_creates_and_replaces(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "f.json"
    atomic_write_bytes(path, b"one")
    atomic_write_bytes(path, b"two")
    assert path.read_bytes() == b"two"
    assert [p.name for p in path.parent.iterdir()] == ["f.json"]


def test_failure_keeps_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f.json"
    atomic_write_bytes(path, b"good")

    def boom(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_bytes(path, b"partial")
    monkeypatch.undo()
    assert path.read_bytes() == b"good"
    assert [p.name for p in tmp_path.iterdir()] == ["f.json"]  # temp file cleaned up
