"""Dead-man's-switch ping (e.g. healthchecks.io) after a successful run."""

from __future__ import annotations

import httpx

from jobfinder.logging_setup import get_logger


async def ping(client: httpx.AsyncClient, url: str) -> bool:
    """GET ``url``; never raises. The URL is a secret and is redacted from logs."""
    try:
        response = await client.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        get_logger("healthcheck").warning("healthcheck_ping_failed", error=type(exc).__name__)
        return False
    if response.status_code >= 400:
        get_logger("healthcheck").warning("healthcheck_ping_failed", status=response.status_code)
        return False
    return True
