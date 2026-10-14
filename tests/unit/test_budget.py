from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jobfinder.ai.budget import BudgetGuard, day_window, month_window
from jobfinder.storage.state_store import SpendEntry, StateStore

NOW = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)


def _spend(store: StateStore, at: datetime, cost: float) -> None:
    store.record_spend(SpendEntry(at, "groq", "m", "score", 1, 0, 1, cost))


def test_windows() -> None:
    assert day_window(NOW) == (datetime(2026, 12, 31, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    assert month_window(NOW) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert month_window(datetime(2026, 2, 10, tzinfo=UTC))[1] == datetime(2026, 3, 1, tzinfo=UTC)


def test_daily_cap(tmp_path: Path) -> None:
    with StateStore(tmp_path / "s.db") as store:
        guard = BudgetGuard(store, daily_cap_usd=1.0, monthly_cap_usd=20.0)
        _spend(store, datetime(2026, 12, 30, 23, 0, tzinfo=UTC), 5.0)  # yesterday
        _spend(store, datetime(2026, 12, 31, 1, 0, tzinfo=UTC), 0.95)
        assert guard.allows(0.05, NOW)
        assert not guard.allows(0.06, NOW)
        # Next day the daily budget is fresh again.
        assert guard.allows(0.5, datetime(2027, 1, 1, 0, 5, tzinfo=UTC))


def test_monthly_cap(tmp_path: Path) -> None:
    with StateStore(tmp_path / "s.db") as store:
        guard = BudgetGuard(store, daily_cap_usd=1.0, monthly_cap_usd=2.0)
        _spend(store, datetime(2026, 12, 5, tzinfo=UTC), 1.0)
        _spend(store, datetime(2026, 12, 20, tzinfo=UTC), 0.9)
        status = guard.status(NOW)
        assert status.spent_today == 0
        assert status.spent_this_month == 1.9
        assert not guard.allows(0.2, NOW)
        assert guard.allows(0.2, datetime(2027, 1, 2, tzinfo=UTC))  # new month


def test_spend_summary(tmp_path: Path) -> None:
    with StateStore(tmp_path / "s.db") as store:
        _spend(store, datetime(2026, 12, 5, tzinfo=UTC), 1.0)
        store.record_spend(
            SpendEntry(datetime(2026, 12, 6, tzinfo=UTC), "g", "m", "eval", 1, 0, 1, 3.0)
        )
        assert store.spend_summary(datetime(2026, 12, 1, tzinfo=UTC), NOW, "score") == (1, 1.0)
