from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jobfinder.models import RunStats, utcnow


def test_utcnow_is_aware() -> None:
    assert utcnow().tzinfo is UTC


def test_run_stats_requires_aware_datetimes() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        RunStats(run_id="r", started_at=datetime(2026, 3, 1))  # noqa: DTZ001
    local = datetime(2026, 3, 1, 14, tzinfo=timezone(timedelta(hours=2)))
    assert RunStats(run_id="r", started_at=local).started_at.tzinfo is UTC


def test_datetimes_truncated_to_milliseconds() -> None:
    from jobfinder.models import to_millis

    precise = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)
    assert to_millis(precise).microsecond == 123000
    assert RunStats(run_id="r", started_at=precise).started_at.microsecond == 123000
    assert utcnow().microsecond % 1000 == 0
