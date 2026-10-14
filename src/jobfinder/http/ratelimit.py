"""Persistent per-host token buckets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobfinder.storage.state_store import StateStore, TokenBucketState


def take_token(
    state: TokenBucketState | None, now: datetime, per_minute: float, capacity: float = 1.0
) -> tuple[TokenBucketState, float]:
    """Reserve one token. Returns the new bucket state and the seconds to wait first.

    The bucket refills at ``per_minute / 60`` tokens per second up to ``capacity``. When
    no token is available the caller is given a reservation: it waits, and the bucket is
    recorded as empty at the moment the wait ends, so concurrent and later callers queue
    behind it, also across process restarts.
    """
    rate = per_minute / 60.0
    if state is None:
        tokens, last = capacity, now
    else:
        last = state.updated_at
        elapsed = max(0.0, (now - last).total_seconds())
        tokens = min(capacity, state.tokens + elapsed * rate)
        last = max(last, now)
    if tokens >= 1.0:
        return TokenBucketState(tokens=tokens - 1.0, updated_at=last), 0.0
    wait = (1.0 - tokens) / rate
    return TokenBucketState(tokens=0.0, updated_at=last + timedelta(seconds=wait)), wait + max(
        0.0, (last - now).total_seconds()
    )


@dataclass
class HostRateLimiter:
    """Applies per-host requests-per-minute limits, persisted in the state store."""

    store: StateStore
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def acquire(self, host: str, per_minute: float) -> float:
        """Wait for a token for ``host``; returns the seconds waited."""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            key = f"host:{host}"
            new_state, wait = take_token(self.store.get_token_bucket(key), self.clock(), per_minute)
            self.store.set_token_bucket(key, new_state)
        if wait > 0:
            await self.sleep(wait)
        return wait
