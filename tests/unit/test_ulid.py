from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobfinder.ulid import new_ulid, ulid_timestamp_ms

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


def test_format_and_timestamp() -> None:
    value = new_ulid(T0)
    assert len(value) == 26
    assert ulid_timestamp_ms(value) == int(T0.timestamp() * 1000)


def test_sorts_by_time_and_is_unique() -> None:
    values = [new_ulid(T0 + timedelta(milliseconds=i)) for i in range(50)]
    assert values == sorted(values)
    assert len({new_ulid(T0) for _ in range(1000)}) == 1000


def test_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_ulid(datetime(2026, 1, 1))  # noqa: DTZ001


def test_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a ULID"):
        ulid_timestamp_ms("not-a-ulid")
