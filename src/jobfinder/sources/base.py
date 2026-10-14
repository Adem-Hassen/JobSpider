"""Source protocol and the context each fetch runs in."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from jobfinder.config import AtsCompany
from jobfinder.http.client import HttpClient, RequestPolicy
from jobfinder.models import RawJob
from jobfinder.storage.state_store import SourceState


@dataclass
class FetchReport:
    """What happened during one source fetch, beyond the jobs returned."""

    errors: list[str] = field(default_factory=list)
    not_modified: list[str] = field(default_factory=list)
    skipped_records: int = 0

    @property
    def complete(self) -> bool:
        """True when every request succeeded, so absent jobs really are absent."""
        return not self.errors


@dataclass
class FetchContext:
    """The ``policy`` a source fetches under: HTTP access, limits, configuration."""

    http: HttpClient
    policy: RequestPolicy
    now: datetime
    queries: list[Mapping[str, Any]] = field(default_factory=list)
    options: Mapping[str, Any] = field(default_factory=dict)
    ats_companies: list[AtsCompany] = field(default_factory=list)
    credentials: Mapping[str, str] = field(default_factory=dict)
    is_known: Callable[[str, str], bool] = field(default=lambda source, job_id: False)
    report: FetchReport = field(default_factory=FetchReport)

    def all_known(self, jobs: list[RawJob]) -> bool:
        """True when every job on a page is already stored (paging can stop)."""
        return bool(jobs) and all(self.is_known(j.source, j.source_job_id) for j in jobs)


class Source(Protocol):
    name: str

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]: ...


# --------------------------------------------------------------------------- parse helpers


def text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def parse_iso(value: Any, *, assume_utc: bool = True) -> datetime | None:
    """ISO-8601 to aware UTC; naive values are taken as UTC when ``assume_utc``."""
    raw = text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not assume_utc:
            return None
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def parse_epoch_s(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def parse_records(
    records: Any, parse_one: Callable[[Mapping[str, Any]], RawJob | None], report: FetchReport
) -> list[RawJob]:
    """Parse each record; a malformed record is skipped and counted, never fatal."""
    if not isinstance(records, list):
        raise TypeError("expected a list of job records")
    jobs = []
    for record in records:
        try:
            job = parse_one(record) if isinstance(record, Mapping) else None
        except (KeyError, TypeError, ValueError):
            report.skipped_records += 1
            continue
        if job is not None:
            jobs.append(job)
    return jobs
