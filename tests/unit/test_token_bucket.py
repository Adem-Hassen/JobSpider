from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobfinder.http.ratelimit import HostRateLimiter, take_token
from jobfinder.storage.state_store import StateStore, TokenBucketState
from tests.helpers import FakeSleep

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


def test_first_request_is_free() -> None:
    state, wait = take_token(None, T0, per_minute=2)
    assert wait == 0
    assert state == TokenBucketState(0.0, T0)


def test_second_request_waits_one_interval() -> None:
    state, _ = take_token(None, T0, per_minute=2)
    state, wait = take_token(state, T0 + timedelta(seconds=10), per_minute=2)
    assert wait == pytest.approx(20.0)  # 2/min -> one token per 30 s
    assert state.updated_at == T0 + timedelta(seconds=30)


def test_reservations_queue_up() -> None:
    state, _ = take_token(None, T0, per_minute=6)  # one per 10 s
    waits = []
    for _ in range(3):
        state, wait = take_token(state, T0, per_minute=6)
        waits.append(wait)
    assert waits == pytest.approx([10.0, 20.0, 30.0])


def test_refills_but_never_beyond_capacity() -> None:
    state, _ = take_token(None, T0, per_minute=60)
    later = T0 + timedelta(hours=1)
    state, wait = take_token(state, later, per_minute=60)
    assert wait == 0
    state, wait = take_token(state, later, per_minute=60)
    assert wait == pytest.approx(1.0)  # capacity 1: no burst after idling


def test_clock_going_backwards_is_safe() -> None:
    state, _ = take_token(None, T0, per_minute=60)
    _, wait = take_token(state, T0 - timedelta(minutes=5), per_minute=60)
    assert wait >= 0


async def test_limiter_persists_across_instances(tmp_path: Path) -> None:
    sleep = FakeSleep()
    with StateStore(tmp_path / "state.db") as store:
        first = HostRateLimiter(store, clock=lambda: T0, sleep=sleep)
        assert await first.acquire("remotive.com", 2) == 0
        # A new process (new limiter) at the same moment must still wait.
        second = HostRateLimiter(store, clock=lambda: T0 + timedelta(seconds=5), sleep=sleep)
        assert await second.acquire("remotive.com", 2) == pytest.approx(25.0)
        assert await second.acquire("other.host", 2) == 0
    assert sleep.calls == [pytest.approx(25.0)]
