"""Running one source: policy, fetch, and the bookkeeping behind pausing.

A run *fails* when the fetch raised, or reported errors without getting any answer.
Three failed runs in a row pause the source for 24 hours (circuit breaker). A 401/403
pauses it at once and raises an alert, since retrying will not fix credentials. A run
stopped by a request cap is neither a success nor a failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from jobfinder.config import SOURCE_TERMS, AppConfig, SourceName
from jobfinder.http.client import CapReachedError, HttpClient, RequestPolicy, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, FetchReport, Source
from jobfinder.storage.state_store import StateStore

FAILURES_BEFORE_PAUSE = 3
BREAKER_PAUSE = timedelta(hours=24)
AUTH_PAUSE = timedelta(hours=24)

_CREDENTIALS: dict[SourceName, tuple[str, ...]] = {
    SourceName.ADZUNA: ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
    SourceName.FRANCE_TRAVAIL: ("FRANCE_TRAVAIL_CLIENT_ID", "FRANCE_TRAVAIL_CLIENT_SECRET"),
    SourceName.EMAIL_ALERTS: ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"),
}


class FetchStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"  # some requests failed, some succeeded
    FAILED = "failed"
    CAPPED = "capped"
    AUTH_FAILED = "auth_failed"


@dataclass
class SourceFetch:
    name: SourceName
    jobs: list[RawJob] = field(default_factory=list)
    report: FetchReport = field(default_factory=FetchReport)
    status: FetchStatus = FetchStatus.OK
    fatal: str | None = None
    alerts: list[str] = field(default_factory=list)


def policy_for(config: AppConfig, name: SourceName) -> RequestPolicy:
    source_config = config.sources.sources[name]
    terms = SOURCE_TERMS.get(name)
    return RequestPolicy(
        source=name.value,
        daily_request_cap=source_config.daily_request_cap,
        requests_per_minute=source_config.requests_per_minute,
        max_per_minute=terms.max_requests_per_minute if terms else None,
        weekly_cap=terms.max_weekly_requests if terms else None,
        monthly_cap=terms.max_monthly_requests if terms else None,
    )


def _credentials(config: AppConfig, name: SourceName) -> dict[str, str]:
    values: dict[str, str] = {}
    for var in _CREDENTIALS.get(name, ()):
        value = getattr(config.secrets, var)
        if value is not None:
            values[var] = value.get_secret_value() if hasattr(value, "get_secret_value") else value
    return values


def build_context(
    config: AppConfig,
    name: SourceName,
    http: HttpClient,
    now: datetime,
    is_known: Callable[[str, str], bool] | None = None,
) -> FetchContext:
    source_config = config.sources.sources[name]
    ctx = FetchContext(
        http=http,
        policy=policy_for(config, name),
        now=now,
        queries=list(source_config.queries),
        options=source_config.options,
        ats_companies=list(config.sources.ats_companies),
        credentials=_credentials(config, name),
    )
    if is_known is not None:
        ctx.is_known = is_known
    return ctx


def record_result(store: StateStore, name: SourceName, result: SourceFetch, now: datetime) -> None:
    """Update the source's state (attempt, success, failures, pause) after a run."""
    state = replace(store.get_source_state(name.value), last_attempt_at=now)
    if result.status in (FetchStatus.OK, FetchStatus.PARTIAL):
        state = replace(state, last_success_at=now, consecutive_failures=0, pause_reason=None)
    elif result.status is FetchStatus.AUTH_FAILED:
        state = replace(
            state,
            consecutive_failures=state.consecutive_failures + 1,
            paused_until=now + AUTH_PAUSE,
            pause_reason=result.fatal,
        )
        result.alerts.append(
            f"{name.value} paused for {AUTH_PAUSE.total_seconds() / 3600:g} h: {result.fatal}. "
            "Check its credentials or access."
        )
    elif result.status is FetchStatus.FAILED:
        failures = state.consecutive_failures + 1
        if failures >= FAILURES_BEFORE_PAUSE:
            state = replace(
                state,
                consecutive_failures=0,
                paused_until=now + BREAKER_PAUSE,
                pause_reason=f"{failures} failed runs in a row; last: {result.fatal}",
            )
            result.alerts.append(
                f"{name.value} paused for {BREAKER_PAUSE.total_seconds() / 3600:g} h after "
                f"{failures} failed runs in a row (last error: {result.fatal})"
            )
        else:
            state = replace(state, consecutive_failures=failures)
    store.save_source_state(state)


async def fetch_source(
    config: AppConfig,
    name: SourceName,
    source: Source,
    http: HttpClient,
    store: StateStore,
    now: datetime,
    *,
    is_known: Callable[[str, str], bool] | None = None,
    record: bool = True,
) -> SourceFetch:
    """Fetch one source. Errors are captured in the result, never raised.

    With ``record`` the source's state (last attempt/success, failures, pause) is
    updated; dry runs pass ``record=False``.
    """
    ctx = build_context(config, name, http, now, is_known)
    result = SourceFetch(name=name, report=ctx.report)
    try:
        result.jobs = await source.fetch(ctx, store.get_source_state(name.value))
    except SourceAuthError as exc:
        result.status, result.fatal = FetchStatus.AUTH_FAILED, str(exc)
    except CapReachedError as exc:
        result.status, result.fatal = FetchStatus.CAPPED, str(exc)
    except Exception as exc:  # one failing source must never abort the others
        result.status, result.fatal = FetchStatus.FAILED, f"{type(exc).__name__}: {exc}"
    else:
        report = ctx.report
        got_answer = bool(result.jobs) or bool(report.not_modified)
        if report.errors and not got_answer:
            result.status, result.fatal = FetchStatus.FAILED, report.errors[0]
        elif report.errors:
            result.status = FetchStatus.PARTIAL
    if record:
        record_result(store, name, result, now)
    return result
