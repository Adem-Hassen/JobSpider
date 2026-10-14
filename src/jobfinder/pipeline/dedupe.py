"""Duplicate detection, applied in order:

1. same source + source_job_id (also via sightings)  -> update last_seen_at
2. same canonical URL                                -> add a sighting
3. same fingerprint (company | title | city)         -> merge; an ATS record becomes canonical
4. same company, title token_set_ratio >= 92, published within 30 days
                                                     -> insert, flagged as possible duplicate

Listings from the *same* source with different ids are distinct postings by that
source's own account (e.g. one remote role advertised separately per country), so
rule 3 flags instead of merging them and rule 4 ignores them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from jobfinder.models import ListingStatus, MatchRule, Opportunity, RemoteType, Sighting
from jobfinder.pipeline.normalise import NormalisedJob, norm_city, norm_title
from jobfinder.storage.repository import OpportunityRepository

FUZZY_THRESHOLD = 92.0
FUZZY_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class DedupeOutcome:
    rule: MatchRule
    opportunity: Opportunity
    created: bool


def _seen(existing: Opportunity, now: datetime) -> Opportunity:
    return existing.model_copy(
        update={
            "last_seen_at": max(existing.last_seen_at, now),
            "listing_status": ListingStatus.ACTIVE,
        }
    )


def _sighting(job: NormalisedJob, opportunity_id: str, rule: MatchRule, now: datetime) -> Sighting:
    return Sighting(
        opportunity_id=opportunity_id,
        source=job.source,
        source_job_id=job.source_job_id,
        posting_url=job.posting_url,
        canonical_url=job.canonical_url,
        seen_at=now,
        match_rule=rule,
    )


def _promote_ats(
    existing: Opportunity, job: NormalisedJob, now: datetime
) -> tuple[Opportunity, Sighting]:
    """Make the ATS listing canonical; keep the previous canonical listing as a sighting."""
    previous = Sighting(
        opportunity_id=existing.opportunity_id,
        source=existing.source,
        source_job_id=existing.source_job_id,
        posting_url=existing.posting_url,
        canonical_url=existing.canonical_url,
        seen_at=now,
        match_rule=MatchRule.FINGERPRINT,
    )
    promoted = existing.model_copy(
        update={
            "source": job.source,
            "source_job_id": job.source_job_id,
            "canonical_url": job.canonical_url,
            "posting_url": job.posting_url,
            "apply_url": job.apply_url or existing.apply_url,
            "published_at": job.published_at or existing.published_at,
            "deadline_at": job.deadline_at or existing.deadline_at,
        }
    )
    return promoted, previous


def _dates_close(a: datetime | None, b: datetime | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= FUZZY_WINDOW


def is_fuzzy_duplicate(job: NormalisedJob, candidate: Opportunity) -> float | None:
    """Title similarity if ``candidate`` is a plausible duplicate of ``job``, else None.

    Beyond the ratio and date window, two guards avoid frequent false positives:
    different known seniority levels, and different known on-site cities.
    """
    if job.seniority and candidate.seniority and job.seniority != candidate.seniority:
        return None
    if (
        job.remote_type is not RemoteType.REMOTE
        and candidate.remote_type is not RemoteType.REMOTE
        and job.location_city
        and candidate.location_city
        and norm_city(job.location_city) != norm_city(candidate.location_city)
    ):
        return None
    job_date = job.published_at
    candidate_date = candidate.published_at or candidate.discovered_at
    if not _dates_close(job_date, candidate_date):
        return None
    score = fuzz.token_set_ratio(job.title_norm, norm_title(candidate.title))
    return score if score >= FUZZY_THRESHOLD else None


def dedupe(job: NormalisedJob, repo: OpportunityRepository, now: datetime) -> DedupeOutcome:
    """Apply the dedupe rules for one job and write the result to ``repo``."""
    existing = repo.get_by_source_key(job.source, job.source_job_id)
    if existing is not None:
        updated = _seen(existing, now)
        repo.upsert(updated)
        return DedupeOutcome(MatchRule.SOURCE_KEY, updated, created=False)

    existing = repo.get_by_canonical_url(job.canonical_url)
    if existing is not None:
        updated = _seen(existing, now)
        repo.upsert(updated)
        repo.add_sighting(_sighting(job, existing.opportunity_id, MatchRule.CANONICAL_URL, now))
        return DedupeOutcome(MatchRule.CANONICAL_URL, updated, created=False)

    existing = repo.get_by_fingerprint(job.fingerprint)
    if existing is not None and existing.source == job.source:
        flagged = job.to_opportunity(now).model_copy(
            update={"is_possible_duplicate": True, "duplicate_of": existing.opportunity_id}
        )
        repo.upsert(flagged)
        return DedupeOutcome(MatchRule.FINGERPRINT, flagged, created=True)
    if existing is not None:
        updated = _seen(existing, now)
        if job.is_ats and not _is_ats_source(existing.source):
            updated, previous = _promote_ats(updated, job, now)
            repo.upsert(updated)
            repo.add_sighting(previous)
        else:
            repo.upsert(updated)
            repo.add_sighting(_sighting(job, existing.opportunity_id, MatchRule.FINGERPRINT, now))
        return DedupeOutcome(MatchRule.FINGERPRINT, updated, created=False)

    created = job.to_opportunity(now)
    scored = [
        (score, candidate)
        for candidate in repo.fuzzy_candidates(job.company_norm)
        if candidate.source != job.source
        and (score := is_fuzzy_duplicate(job, candidate)) is not None
    ]
    if scored:
        _, best = max(scored, key=lambda pair: (pair[0], pair[1].discovered_at))
        created = created.model_copy(
            update={"is_possible_duplicate": True, "duplicate_of": best.opportunity_id}
        )
        repo.upsert(created)
        return DedupeOutcome(MatchRule.FUZZY, created, created=True)

    repo.upsert(created)
    return DedupeOutcome(MatchRule.NEW, created, created=True)


ATS_SOURCES = frozenset({"greenhouse", "lever", "ashby"})


def _is_ats_source(source: str) -> bool:
    return source in ATS_SOURCES
