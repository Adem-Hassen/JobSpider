"""Which sources are due on this run."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobfinder.config import SOURCE_TERMS, AppConfig, SourceName
from jobfinder.storage.state_store import StateStore

# Hourly timers fire with jitter; allow a run slightly early so a 60-minute interval
# does not slip to every other tick.
INTERVAL_SLACK = timedelta(minutes=5)


def _effective_interval(name: SourceName, configured_minutes: int) -> timedelta:
    """Configured interval minus the jitter slack, but never below the source's terms."""
    terms = SOURCE_TERMS.get(name)
    floor = timedelta(minutes=terms.min_interval_minutes or 0) if terms else timedelta(0)
    return max(timedelta(minutes=configured_minutes) - INTERVAL_SLACK, floor)


@dataclass(frozen=True)
class SourceDecision:
    name: SourceName
    due: bool
    reason: str


def plan(
    config: AppConfig, store: StateStore, implemented: Collection[SourceName], now: datetime
) -> list[SourceDecision]:
    decisions = []
    today = now.astimezone(UTC).date()
    for name, source_config in config.sources.sources.items():
        state = store.get_source_state(name.value)
        if not source_config.enabled:
            decision = SourceDecision(name, False, "disabled")
        elif name not in implemented:
            decision = SourceDecision(name, False, "not implemented")
        elif state.paused_until is not None and state.paused_until > now:
            decision = SourceDecision(
                name,
                False,
                f"paused until {state.paused_until:%Y-%m-%d %H:%M} UTC: {state.pause_reason}",
            )
        elif (
            state.last_attempt_at is not None
            and now - state.last_attempt_at
            < _effective_interval(name, source_config.min_interval_minutes)
        ):
            next_at = state.last_attempt_at + timedelta(minutes=source_config.min_interval_minutes)
            decision = SourceDecision(name, False, f"next due {next_at:%H:%M} UTC")
        elif store.get_counter(f"source:{name.value}", today) >= source_config.daily_request_cap:
            decision = SourceDecision(name, False, "daily request cap used up")
        else:
            decision = SourceDecision(name, True, "due")
        decisions.append(decision)
    return decisions


def due_sources(
    config: AppConfig, store: StateStore, implemented: Collection[SourceName], now: datetime
) -> list[SourceName]:
    return [d.name for d in plan(config, store, implemented, now) if d.due]
