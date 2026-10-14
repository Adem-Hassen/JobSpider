"""Health checks for ``jobfinder selfcheck``."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from jobfinder.config import AppConfig
from jobfinder.http.identity import user_agent
from jobfinder.locking import LockHeldError, exclusive_lock
from jobfinder.storage.state_store import StateStore, StateStoreError

MIN_FREE_BYTES = 500 * 1024 * 1024


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def check_data_dir(data_dir: Path) -> CheckResult:
    name = "data_dir"
    if not data_dir.is_dir():
        return CheckResult(
            name, Status.FAIL, f"{data_dir} does not exist (create it or run deploy/install.sh)"
        )
    if not os.access(data_dir, os.W_OK | os.X_OK):
        return CheckResult(name, Status.FAIL, f"{data_dir} is not writable by this user")
    free = shutil.disk_usage(data_dir).free
    if free < MIN_FREE_BYTES:
        return CheckResult(name, Status.WARN, f"only {free // (1024 * 1024)} MiB free")
    return CheckResult(name, Status.OK, str(data_dir))


def check_state_db(path: Path) -> CheckResult:
    name = "state_db"
    try:
        with StateStore(path) as store:
            verdict = store.integrity_check()
            version = store.schema_version()
    except (StateStoreError, OSError) as exc:
        return CheckResult(name, Status.FAIL, str(exc))
    except Exception as exc:  # sqlite corruption surfaces as driver-specific errors
        return CheckResult(name, Status.FAIL, f"{type(exc).__name__}: {exc}")
    if verdict != "ok":
        return CheckResult(name, Status.FAIL, f"integrity_check: {verdict}")
    return CheckResult(name, Status.OK, f"{path} (schema v{version})")


def check_run_lock(path: Path) -> CheckResult:
    name = "run_lock"
    try:
        with exclusive_lock(path):
            pass
    except LockHeldError:
        return CheckResult(name, Status.WARN, "held by another process (a run is in progress?)")
    except OSError as exc:
        return CheckResult(name, Status.FAIL, f"cannot open {path}: {exc.strerror}")
    return CheckResult(name, Status.OK, str(path))


def check_optional_integrations(config: AppConfig) -> list[CheckResult]:
    s = config.secrets
    return [
        CheckResult(
            "telegram",
            Status.OK if s.TELEGRAM_BOT_TOKEN else Status.WARN,
            "configured" if s.TELEGRAM_BOT_TOKEN else "not configured; no alerts will be sent",
        ),
        CheckResult(
            "healthcheck",
            Status.OK if s.HEALTHCHECK_URL else Status.WARN,
            "configured" if s.HEALTHCHECK_URL else "HEALTHCHECK_URL not set; no dead-man ping",
        ),
    ]


async def fetch_model_ids(client: httpx.AsyncClient, base_url: str, api_key: str) -> set[str]:
    """GET {base_url}/models on an OpenAI-compatible API and return the model IDs."""
    response = await client.get(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    response.raise_for_status()
    payload = response.json()
    return {str(item["id"]) for item in payload.get("data", []) if "id" in item}


async def check_ai_models(config: AppConfig, client: httpx.AsyncClient) -> CheckResult:
    name = f"ai_models[{config.ai_provider}]"
    wanted = sorted({config.score_model, config.profile_model})
    try:
        available = await fetch_model_ids(
            client, config.provider.base_url, config.ai_api_key.get_secret_value()
        )
    except httpx.HTTPStatusError as exc:
        hint = " (check the API key)" if exc.response.status_code in (401, 403) else ""
        return CheckResult(name, Status.FAIL, f"GET /models -> {exc.response.status_code}{hint}")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return CheckResult(name, Status.FAIL, f"GET /models failed: {type(exc).__name__}: {exc}")
    missing = [m for m in wanted if m not in available]
    if missing:
        return CheckResult(name, Status.FAIL, f"not offered by the provider: {', '.join(missing)}")
    return CheckResult(name, Status.OK, ", ".join(wanted))


async def run_selfcheck(
    config: AppConfig, *, offline: bool, client: httpx.AsyncClient | None = None
) -> list[CheckResult]:
    results = [CheckResult("config", Status.OK, str(config.config_dir))]
    data_dir_result = check_data_dir(config.data_dir)
    results.append(data_dir_result)
    if data_dir_result.status is Status.FAIL:
        results.append(CheckResult("state_db", Status.SKIP, "data_dir unusable"))
        results.append(CheckResult("run_lock", Status.SKIP, "data_dir unusable"))
    else:
        results.append(check_state_db(config.state_db_path))
        results.append(check_run_lock(config.run_lock_path))
    results.extend(check_optional_integrations(config))
    if offline:
        results.append(CheckResult(f"ai_models[{config.ai_provider}]", Status.SKIP, "--offline"))
        return results
    if client is not None:
        results.append(await check_ai_models(config, client))
        return results
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent(config.secrets.CONTACT_EMAIL)},
        timeout=httpx.Timeout(30.0, connect=10.0),
    ) as own_client:
        results.append(await check_ai_models(config, own_client))
    return results


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.status is Status.FAIL for r in results)
