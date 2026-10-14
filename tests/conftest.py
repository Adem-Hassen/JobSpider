from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobfinder.config import AppConfig, Secrets, load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"
ENV_VARS = [*Secrets.model_fields]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Start every test with no jobfinder variables and outside the repo (no .env)."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the shipped config directory."""
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, target)
    return target


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch, config_dir: Path, data_dir: Path) -> dict[str, str]:
    env = {
        "AI_PROVIDER": "groq",
        "GROQ_API_KEY": "gsk_test_secret_value",
        "CONTACT_EMAIL": "me@example.com",
        "DATA_DIR": str(data_dir),
        "JOBFINDER_CONFIG_DIR": str(config_dir),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def edit_yaml(config_dir: Path) -> Callable[[str, Callable[[dict[str, Any]], None]], None]:
    """Load a config YAML, mutate it with ``fn`` and write it back."""

    def _edit(filename: str, fn: Callable[[dict[str, Any]], None]) -> None:
        path = config_dir / filename
        data = yaml.safe_load(path.read_text()) or {}
        fn(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    return _edit


@pytest.fixture
def app_config(base_env: dict[str, str]) -> AppConfig:
    return load_config(env_file=None)


@pytest.fixture(autouse=True)
def restore_tz() -> Iterator[None]:
    """Re-read TZ after tests that change it via monkeypatch."""
    yield
    time.tzset()
