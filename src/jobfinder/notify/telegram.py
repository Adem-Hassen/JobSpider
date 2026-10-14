"""Telegram Bot API: sendMessage to one chat.

The bot token is part of the URL path, so errors raised here never include the URL;
logs are additionally scrubbed of secret values by logging_setup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 60.0
SEND_SPACING_S = 1.1  # Telegram allows about one message per second per chat


class NotifyError(Exception):
    """A message could not be sent. The text never contains the bot token."""


@dataclass
class TelegramNotifier:
    client: httpx.AsyncClient
    token: str
    chat_id: str
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _sent_any: bool = False

    async def send(self, text: str) -> None:
        if self._sent_any:
            await self.sleep(SEND_SPACING_S)
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        last_error = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self.client.post(
                    f"{API}/bot{self.token}/sendMessage", json=payload, timeout=20.0
                )
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__  # str(exc) may contain the URL
                await self.sleep(2.0 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 200 and data.get("ok"):
                self._sent_any = True
                return
            description = str(data.get("description") or f"HTTP {response.status_code}")
            last_error = description.replace(self.token, "***")
            if response.status_code == 429:
                retry_after = float((data.get("parameters") or {}).get("retry_after") or 5)
                if retry_after > MAX_RETRY_AFTER_S:
                    break
                await self.sleep(retry_after)
                continue
            if response.status_code >= 500:
                await self.sleep(2.0 * (attempt + 1))
                continue
            break  # 400 (bad chat id or markup), 401 (bad token), 403 (bot blocked)
        raise NotifyError(f"Telegram sendMessage failed: {last_error}")
