from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.ai.examples import PROFILE_EXAMPLE
from jobfinder.cv.profile_store import ProfileStore, ProfileStoreError
from jobfinder.models import ProfileRecord


def _record(version: int) -> ProfileRecord:
    return ProfileRecord(
        version=version,
        created_at=datetime(2026, 10, 14, tzinfo=UTC),
        cv_sha256="a" * 64,
        cv_filename="cv.pdf",
        overrides_sha256="b" * 64,
        ai_provider="groq",
        ai_model="openai/gpt-oss-120b",
        extracted=PROFILE_EXAMPLE,
        profile=PROFILE_EXAMPLE,
    )


def test_empty(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    assert store.versions() == []
    assert store.latest() is None
    assert store.next_version() == 1


def test_save_and_load(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    store.save(_record(1))
    path = store.save(_record(2))
    (tmp_path / "profiles" / "notes.txt").write_text("ignored")
    assert path.name == "profile_v2.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.versions() == [1, 2]
    assert store.latest() == _record(2)
    assert store.next_version() == 3


def test_versions_sort_numerically(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    for v in (2, 10, 9):
        store.save(_record(v))
    assert store.versions() == [2, 9, 10]


def test_never_overwrites(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.save(_record(1))
    with pytest.raises(ProfileStoreError, match="never overwritten"):
        store.save(_record(1))


def test_invalid_file(tmp_path: Path) -> None:
    (tmp_path / "profile_v1.json").write_text("{}")
    with pytest.raises(ProfileStoreError, match="not a valid profile file"):
        ProfileStore(tmp_path).latest()


def test_missing_version(tmp_path: Path) -> None:
    with pytest.raises(ProfileStoreError, match="does not exist"):
        ProfileStore(tmp_path).load(4)
