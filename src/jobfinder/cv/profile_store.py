"""Versioned profile files: <data_dir>/profiles/profile_v{N}.json."""

from __future__ import annotations

import re
from pathlib import Path

from jobfinder.models import ProfileRecord
from jobfinder.storage.atomic import atomic_write_bytes

_FILE = re.compile(r"^profile_v(\d+)\.json$")


class ProfileStoreError(Exception):
    pass


class ProfileStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, version: int) -> Path:
        return self.directory / f"profile_v{version}.json"

    def versions(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = (_FILE.match(p.name) for p in self.directory.iterdir())
        return sorted(int(m.group(1)) for m in found if m)

    def next_version(self) -> int:
        versions = self.versions()
        return versions[-1] + 1 if versions else 1

    def load(self, version: int) -> ProfileRecord:
        path = self.path_for(version)
        try:
            return ProfileRecord.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            raise ProfileStoreError(f"profile v{version} does not exist") from None
        except ValueError as exc:
            raise ProfileStoreError(f"{path} is not a valid profile file: {exc}") from exc

    def latest(self) -> ProfileRecord | None:
        versions = self.versions()
        return self.load(versions[-1]) if versions else None

    def save(self, record: ProfileRecord) -> Path:
        path = self.path_for(record.version)
        if path.exists():
            raise ProfileStoreError(f"{path} already exists; versions are never overwritten")
        atomic_write_bytes(path, record.model_dump_json(indent=2).encode() + b"\n")
        return path
