"""Daily and monthly AI spend caps (UTC calendar day and month)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobfinder.storage.state_store import StateStore


def day_window(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def month_window(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following


@dataclass(frozen=True)
class BudgetStatus:
    spent_today: float
    spent_this_month: float
    daily_cap: float
    monthly_cap: float

    def allows(self, estimate: float) -> bool:
        return (
            self.spent_today + estimate <= self.daily_cap
            and self.spent_this_month + estimate <= self.monthly_cap
        )


@dataclass(frozen=True)
class BudgetGuard:
    store: StateStore
    daily_cap_usd: float
    monthly_cap_usd: float

    def status(self, now: datetime) -> BudgetStatus:
        return BudgetStatus(
            spent_today=self.store.spend_between(*day_window(now)),
            spent_this_month=self.store.spend_between(*month_window(now)),
            daily_cap=self.daily_cap_usd,
            monthly_cap=self.monthly_cap_usd,
        )

    def allows(self, estimate: float, now: datetime) -> bool:
        return self.status(now).allows(estimate)
