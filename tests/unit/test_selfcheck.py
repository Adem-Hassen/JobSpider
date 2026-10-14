from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx

from jobfinder.config import AppConfig
from jobfinder.http.identity import user_agent
from jobfinder.locking import exclusive_lock
from jobfinder.selfcheck import (
    Status,
    check_data_dir,
    check_run_lock,
    check_state_db,
    has_failures,
    run_selfcheck,
)

MODELS_URL = "https://api.groq.com/openai/v1/models"


def _by_name(results: list) -> dict[str, Status]:  # type: ignore[type-arg]
    return {r.name: r.status for r in results}


def test_user_agent() -> None:
    assert user_agent("me@example.com").endswith("(+mailto:me@example.com)")
    assert user_agent("me@example.com").startswith("jobfinder/")


def test_data_dir_missing(tmp_path: Path) -> None:
    result = check_data_dir(tmp_path / "missing")
    assert result.status is Status.FAIL
    assert "install.sh" in result.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_data_dir_not_writable(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    try:
        assert check_data_dir(tmp_path).status is Status.FAIL
    finally:
        tmp_path.chmod(0o700)


def test_corrupt_state_db(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    result = check_state_db(path)
    assert result.status is Status.FAIL


def test_lock_held_is_a_warning(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with exclusive_lock(path):
        assert check_run_lock(path).status is Status.WARN


async def test_offline(app_config: AppConfig) -> None:
    results = await run_selfcheck(app_config, offline=True)
    statuses = _by_name(results)
    assert statuses["config"] is Status.OK
    assert statuses["data_dir"] is Status.OK
    assert statuses["state_db"] is Status.OK
    assert statuses["run_lock"] is Status.OK
    assert statuses["telegram"] is Status.WARN
    assert statuses["ai_models[groq]"] is Status.SKIP
    assert not has_failures(results)


@respx.mock
async def test_models_listed(app_config: AppConfig) -> None:
    route = respx.get(MODELS_URL).respond(
        json={"object": "list", "data": [{"id": "openai/gpt-oss-120b"}, {"id": "other"}]}
    )
    async with httpx.AsyncClient() as client:
        results = await run_selfcheck(app_config, offline=False, client=client)
    assert _by_name(results)["ai_models[groq]"] is Status.OK
    assert route.calls.last.request.headers["Authorization"] == "Bearer gsk_test_secret_value"
    assert not has_failures(results)


@respx.mock
async def test_model_not_listed_fails(app_config: AppConfig) -> None:
    respx.get(MODELS_URL).respond(json={"data": [{"id": "something-else"}]})
    async with httpx.AsyncClient() as client:
        results = await run_selfcheck(app_config, offline=False, client=client)
    (ai,) = [r for r in results if r.name == "ai_models[groq]"]
    assert ai.status is Status.FAIL
    assert "openai/gpt-oss-120b" in ai.detail
    assert has_failures(results)


@respx.mock
async def test_bad_key_hint(app_config: AppConfig) -> None:
    respx.get(MODELS_URL).respond(status_code=401, json={"error": "invalid key"})
    async with httpx.AsyncClient() as client:
        results = await run_selfcheck(app_config, offline=False, client=client)
    (ai,) = [r for r in results if r.name == "ai_models[groq]"]
    assert ai.status is Status.FAIL
    assert "401 (check the API key)" in ai.detail


@respx.mock
async def test_network_error(app_config: AppConfig) -> None:
    respx.get(MODELS_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    async with httpx.AsyncClient() as client:
        results = await run_selfcheck(app_config, offline=False, client=client)
    assert _by_name(results)["ai_models[groq]"] is Status.FAIL


async def test_missing_data_dir_skips_dependent_checks(
    app_config: AppConfig, data_dir: Path
) -> None:
    data_dir.rmdir()  # noqa: ASYNC240
    results = await run_selfcheck(app_config, offline=True)
    statuses = _by_name(results)
    assert statuses["data_dir"] is Status.FAIL
    assert statuses["state_db"] is Status.SKIP
    assert not (data_dir / "state.db").exists()
