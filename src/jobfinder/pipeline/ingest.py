"""Normalise fetched jobs, dedupe them against the repository and store the result."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from jobfinder.models import MatchRule, RawJob
from jobfinder.pipeline.dedupe import DedupeOutcome, dedupe
from jobfinder.pipeline.normalise import NormalisedJob, normalise
from jobfinder.storage.repository import OpportunityRepository


@dataclass
class IngestResult:
    outcomes: list[tuple[NormalisedJob, DedupeOutcome]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    out_of_scope: int = 0  # dropped before storage by the ``scope`` check

    @property
    def by_rule(self) -> Counter[MatchRule]:
        return Counter(outcome.rule for _, outcome in self.outcomes)

    @property
    def new(self) -> list[tuple[NormalisedJob, DedupeOutcome]]:
        return [(job, outcome) for job, outcome in self.outcomes if outcome.created]


def ingest(
    raw_jobs: Iterable[RawJob],
    repo: OpportunityRepository,
    *,
    preferred_countries: list[str],
    now: datetime,
    scope: Callable[[NormalisedJob], str | None] | None = None,
) -> IngestResult:
    """Process jobs one by one, so duplicates within the same batch are caught too.

    ``scope`` returns a reason for jobs that must not be stored at all (e.g. located in
    a country you did not target); those are counted and dropped before dedupe.
    """
    result = IngestResult()
    for raw in raw_jobs:
        try:
            job = normalise(raw, preferred_countries)
            if scope is not None and scope(job) is not None:
                result.out_of_scope += 1
                continue
            outcome = dedupe(job, repo, now)
        except (ValueError, ValidationError) as exc:
            result.errors.append(f"{raw.source}/{raw.source_job_id}: {exc}")
            continue
        result.outcomes.append((job, outcome))
    return result
