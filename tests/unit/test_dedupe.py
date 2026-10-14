from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobfinder.models import MatchRule, RawJob, RemoteType
from jobfinder.pipeline.dedupe import dedupe
from jobfinder.pipeline.normalise import normalise
from jobfinder.storage.excel_repo import ExcelRepository
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.sql_repo import SqlRepository

T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "excel"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[OpportunityRepository]:
    if request.param == "sqlite":
        r: OpportunityRepository = SqlRepository.sqlite(tmp_path / "o.db")
    else:
        r = ExcelRepository(tmp_path / "o.xlsx")
    yield r
    r.close()


def raw(**kw: object) -> RawJob:
    data: dict[str, object] = {
        "source": "remotive",
        "source_job_id": "r1",
        "is_ats": False,
        "title": "Backend Engineer",
        "company": "Acme",
        "locations": ["Berlin"],
        "posting_url": "https://remotive.com/remote-jobs/dev/backend-engineer-r1",
        "published_at": T0,
    }
    data.update(kw)
    return RawJob.model_validate(data)


def run(repo: OpportunityRepository, job: RawJob, now: datetime = T0):  # type: ignore[no-untyped-def]
    return dedupe(normalise(job, ["DE"]), repo, now)


# --------------------------------------------------------------------------- rule 1


def test_rule1_same_source_key_updates_last_seen(repo: OpportunityRepository) -> None:
    first = run(repo, raw())
    later = T0 + timedelta(hours=5)
    second = run(repo, raw(title="Backend Engineer (renamed)"), later)
    assert second.rule is MatchRule.SOURCE_KEY
    assert not second.created
    assert second.opportunity.opportunity_id == first.opportunity.opportunity_id
    assert second.opportunity.last_seen_at == later
    assert repo.count() == 1


def test_rule1_finds_listing_merged_into_another(repo: OpportunityRepository) -> None:
    ats = run(
        repo,
        raw(
            source="greenhouse",
            source_job_id="g1",
            is_ats=True,
            posting_url="https://boards.greenhouse.io/acme/jobs/g1",
        ),
    )
    run(repo, raw())  # merged by fingerprint as a sighting
    again = run(repo, raw(), T0 + timedelta(hours=1))
    assert again.rule is MatchRule.SOURCE_KEY
    assert again.opportunity.opportunity_id == ats.opportunity.opportunity_id
    assert len(repo.sightings_for(ats.opportunity.opportunity_id)) == 1


def test_rule1_same_id_other_source_is_not_a_match(repo: OpportunityRepository) -> None:
    run(repo, raw(source_job_id="42"))
    other = run(
        repo,
        raw(
            source="lever",
            source_job_id="42",
            company="Globex",
            posting_url="https://jobs.lever.co/globex/42",
        ),
    )
    assert other.rule is MatchRule.NEW


# --------------------------------------------------------------------------- rule 2


def test_rule2_canonical_url_adds_sighting(repo: OpportunityRepository) -> None:
    first = run(
        repo,
        raw(
            source="greenhouse",
            source_job_id="g1",
            is_ats=True,
            posting_url="https://boards.greenhouse.io/acme/jobs/g1",
        ),
    )
    second = run(
        repo,
        raw(
            source="arbeitnow",
            source_job_id="a9",
            title="Something else",
            posting_url="https://Boards.Greenhouse.io/acme/jobs/g1/?utm_source=x",
        ),
    )
    assert second.rule is MatchRule.CANONICAL_URL
    assert second.opportunity.opportunity_id == first.opportunity.opportunity_id
    (sighting,) = repo.sightings_for(first.opportunity.opportunity_id)
    assert sighting.source == "arbeitnow"
    assert sighting.match_rule is MatchRule.CANONICAL_URL
    assert repo.count() == 1


def test_rule2_different_query_param_is_different_url(repo: OpportunityRepository) -> None:
    run(repo, raw(posting_url="https://x.io/job?id=1", title="A"))
    other = run(repo, raw(source_job_id="r2", posting_url="https://x.io/job?id=2", title="B"))
    assert other.rule is MatchRule.NEW


# --------------------------------------------------------------------------- rule 3


def test_rule3_fingerprint_merges_with_gender_marker_and_suffix(
    repo: OpportunityRepository,
) -> None:
    first = run(repo, raw())
    second = run(
        repo,
        raw(
            source="arbeitnow",
            source_job_id="a1",
            company="Acme GmbH",
            title="Backend Engineer (m/w/d)",
            posting_url="https://www.arbeitnow.com/jobs/acme/backend-a1",
        ),
    )
    assert second.rule is MatchRule.FINGERPRINT
    assert second.opportunity.opportunity_id == first.opportunity.opportunity_id
    assert second.opportunity.source == "remotive"  # neither is ATS: first stays canonical
    assert repo.count() == 1


def test_rule3_ats_record_becomes_canonical(repo: OpportunityRepository) -> None:
    aggregator = run(repo, raw())
    ats = run(
        repo,
        raw(
            source="lever",
            source_job_id="L1",
            is_ats=True,
            posting_url="https://jobs.lever.co/acme/L1",
            apply_url="https://jobs.lever.co/acme/L1/apply",
        ),
    )
    assert ats.rule is MatchRule.FINGERPRINT
    merged = ats.opportunity
    assert merged.opportunity_id == aggregator.opportunity.opportunity_id
    assert (merged.source, merged.source_job_id) == ("lever", "L1")
    assert merged.posting_url == "https://jobs.lever.co/acme/L1"
    assert merged.apply_url == "https://jobs.lever.co/acme/L1/apply"
    (sighting,) = repo.sightings_for(merged.opportunity_id)
    assert (sighting.source, sighting.source_job_id) == ("remotive", "r1")
    # Both listings still resolve to the same opportunity on the next run.
    assert run(repo, raw(), T0 + timedelta(hours=1)).rule is MatchRule.SOURCE_KEY
    assert repo.count() == 1


def test_rule3_ats_stays_canonical_when_aggregator_arrives_later(
    repo: OpportunityRepository,
) -> None:
    ats = run(
        repo,
        raw(
            source="greenhouse",
            source_job_id="g1",
            is_ats=True,
            posting_url="https://boards.greenhouse.io/acme/jobs/g1",
            apply_url="https://boards.greenhouse.io/acme/jobs/g1",
        ),
    )
    later = run(repo, raw())
    assert later.opportunity.source == "greenhouse"
    assert later.opportunity.apply_url == ats.opportunity.apply_url


@pytest.mark.parametrize(
    "change",
    [
        {"locations": ["Paris"]},  # different city
        {"company": "Globex"},  # different company
        {"title": "Frontend Engineer"},  # different title
        {"locations": ["Remote"], "remote_hint": "remote"},  # remote vs on-site Berlin
    ],
)
def test_rule3_false_positives(repo: OpportunityRepository, change: dict[str, object]) -> None:
    run(repo, raw())
    other = run(
        repo,
        raw(
            source="arbeitnow",
            source_job_id="a1",
            posting_url="https://www.arbeitnow.com/jobs/x-a1",
            **change,
        ),
    )
    assert other.rule in (MatchRule.NEW, MatchRule.FUZZY)
    assert other.created
    assert repo.count() == 2


# --------------------------------------------------------------------------- rule 4


def test_rule4_fuzzy_flags_possible_duplicate(repo: OpportunityRepository) -> None:
    first = run(repo, raw(title="Backend Engineer Payments Platform"))
    second = run(
        repo,
        raw(
            source="arbeitnow",
            source_job_id="a1",
            title="Payments Platform Backend Engineer - Core",
            posting_url="https://www.arbeitnow.com/jobs/x-a1",
            published_at=T0 + timedelta(days=10),
        ),
    )
    assert second.rule is MatchRule.FUZZY
    assert second.created
    assert second.opportunity.is_possible_duplicate
    assert second.opportunity.duplicate_of == first.opportunity.opportunity_id
    assert repo.count() == 2


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"published_at": T0 + timedelta(days=45)}, "outside the 30-day window"),
        ({"title": "Senior Backend Engineer Payments Platform"}, "different seniority"),
        ({"title": "Payments Platform Data Analyst"}, "ratio below 92"),
        ({"company": "Globex"}, "different company"),
        ({"locations": ["Munich"]}, "different on-site city"),
        ({"published_at": None}, "no publication date to compare"),
    ],
)
def test_rule4_false_positives(
    repo: OpportunityRepository, change: dict[str, object], why: str
) -> None:
    run(repo, raw(title="Junior Backend Engineer Payments Platform"))
    data: dict[str, object] = {
        "source": "arbeitnow",
        "source_job_id": "a1",
        "title": "Junior Backend Engineer Payments Platform (Core)",
        "posting_url": "https://www.arbeitnow.com/jobs/x-a1",
    }
    data.update(change)
    other = run(repo, raw(**data))
    assert other.rule is MatchRule.NEW, why
    assert not other.opportunity.is_possible_duplicate


def test_rule4_remote_jobs_ignore_city_guard(repo: OpportunityRepository) -> None:
    run(repo, raw(title="Backend Engineer Payments", remote_hint="remote", locations=["Berlin"]))
    other = run(
        repo,
        raw(
            source="arbeitnow",
            source_job_id="a1",
            remote_hint="remote",
            locations=["Munich"],
            title="Backend Engineer Payments Core",
            posting_url="https://www.arbeitnow.com/jobs/x-a1",
        ),
    )
    assert other.opportunity.remote_type is RemoteType.REMOTE
    assert other.rule is MatchRule.FUZZY


def test_within_batch_duplicates(repo: OpportunityRepository) -> None:
    assert run(repo, raw()).rule is MatchRule.NEW
    assert run(repo, raw()).rule is MatchRule.SOURCE_KEY
    assert repo.count() == 1


# --------------------------------------------------------------------------- same source


def test_same_source_same_fingerprint_is_flagged_not_merged(repo: OpportunityRepository) -> None:
    """GitLab advertises one remote role separately per country, each with its own id."""
    us = run(
        repo,
        raw(
            source="greenhouse",
            source_job_id="g-us",
            is_ats=True,
            remote_hint="remote",
            locations=["Remote, United States"],
            title="Senior Solutions Architect",
            posting_url="https://boards.greenhouse.io/acme/jobs/g-us",
        ),
    )
    kr = run(
        repo,
        raw(
            source="greenhouse",
            source_job_id="g-kr",
            is_ats=True,
            remote_hint="remote",
            locations=["Remote, South Korea"],
            title="Senior Solutions Architect",
            posting_url="https://boards.greenhouse.io/acme/jobs/g-kr",
        ),
    )
    assert kr.rule is MatchRule.FINGERPRINT
    assert kr.created
    assert kr.opportunity.is_possible_duplicate
    assert kr.opportunity.duplicate_of == us.opportunity.opportunity_id
    assert kr.opportunity.location_country == "KR"
    assert repo.count() == 2
    assert repo.sightings_for(us.opportunity.opportunity_id) == []


def test_fuzzy_ignores_same_source_candidates(repo: OpportunityRepository) -> None:
    run(
        repo,
        raw(
            source="lever",
            source_job_id="L1",
            is_ats=True,
            title="Software Engineer, Internship",
            posting_url="https://jobs.lever.co/acme/L1",
        ),
    )
    other = run(
        repo,
        raw(
            source="lever",
            source_job_id="L2",
            is_ats=True,
            title="Forward Deployed Software Engineer, Internship",
            posting_url="https://jobs.lever.co/acme/L2",
        ),
    )
    assert other.rule is MatchRule.NEW
    assert not other.opportunity.is_possible_duplicate


# --------------------------------------------------------------------------- scope


def test_ingest_drops_out_of_scope_jobs(repo: OpportunityRepository) -> None:
    from jobfinder.pipeline.ingest import ingest

    jobs = [
        raw(source_job_id="de", locations=["Berlin"], posting_url="https://x.io/de"),
        raw(
            source_job_id="us",
            locations=["Austin, TX"],
            title="Ops Engineer",
            posting_url="https://x.io/us",
        ),
        raw(
            source_job_id="emea",
            locations=["Remote, EMEA"],
            remote_hint="remote",
            title="SRE",
            posting_url="https://x.io/emea",
        ),
    ]
    result = ingest(
        jobs,
        repo,
        preferred_countries=["DE"],
        now=T0,
        scope=lambda job: None if job.location_country in (None, "DE") else "elsewhere",
    )
    assert result.out_of_scope == 1
    assert {job.source_job_id for job, _ in result.outcomes} == {"de", "emea"}
    assert repo.count() == 2
