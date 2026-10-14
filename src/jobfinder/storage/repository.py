"""The storage contract shared by the Excel and SQL backends."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from jobfinder.models import (
    ListingStatus,
    Opportunity,
    ProfileRecord,
    RunStats,
    Sighting,
    UserFields,
)


class OpportunityRepository(Protocol):
    """Opportunity storage. Lookups also consider sightings, so a listing merged into
    another opportunity is still found by its own source key and URL.

    ``upsert`` never overwrites the user-owned fields (application_status, notes) of an
    existing opportunity.
    """

    def get_by_source_key(self, source: str, source_job_id: str) -> Opportunity | None: ...

    def get_by_canonical_url(self, url: str) -> Opportunity | None: ...

    def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None: ...

    def fuzzy_candidates(self, company_norm: str) -> list[Opportunity]: ...

    def upsert(self, opportunity: Opportunity) -> None: ...

    def add_sighting(self, sighting: Sighting) -> None: ...

    def mark_status(self, opportunity_id: str, status: ListingStatus) -> None: ...

    def iter_active(self) -> Iterator[Opportunity]: ...

    def iter_all(self) -> Iterator[Opportunity]:
        """Every opportunity, oldest first, whatever its listing status."""
        ...

    def user_fields(self, opportunity_id: str) -> UserFields | None: ...

    def sightings_for(self, opportunity_id: str) -> list[Sighting]: ...

    def count(self) -> int: ...

    def has_source(self, source: str) -> bool:
        """Whether any opportunity or sighting from ``source`` is stored."""
        ...

    def append_run(self, stats: RunStats) -> None: ...

    def write_profile(self, record: ProfileRecord) -> None:
        """Replace the Profile sheet/table with this profile version."""
        ...

    def flush(self) -> None:
        """Persist pending changes (a no-op for backends that write immediately)."""
        ...

    def close(self) -> None: ...
