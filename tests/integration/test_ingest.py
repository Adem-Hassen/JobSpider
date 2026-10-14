"""Fetch -> normalise -> dedupe -> store, end to end with recorded responses."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from jobfinder.config import AppConfig, SourceName
from jobfinder.http.client import HttpClient, create_async_client
from jobfinder.models import MatchRule
from jobfinder.pipeline.fetch import fetch_source
from jobfinder.pipeline.ingest import ingest
from jobfinder.sources.registry import implemented_sources
from jobfinder.storage.excel_repo import ExcelRepository
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.sql_repo import SqlRepository
from jobfinder.storage.state_store import StateStore
from tests.helpers import FakeClock, load_source_fixture

T0 = datetime(2026, 10, 14, 9, tzinfo=UTC)
ENDPOINTS = {
    SourceName.GREENHOUSE: (
        "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs",
        "greenhouse_gitlab",
    ),
    SourceName.LEVER: ("https://api.lever.co/v0/postings/palantir", "lever_palantir"),
    SourceName.ASHBY: ("https://api.ashbyhq.com/posting-api/job-board/ramp", "ashby_ramp"),
    SourceName.REMOTIVE: ("https://remotive.com/api/remote-jobs", "remotive_software_dev"),
}


@pytest.fixture(params=["sqlite", "excel"])
def repo_factory(request: pytest.FixtureRequest, data_dir: Path) -> Iterator[Any]:
    opened: list[OpportunityRepository] = []

    def factory() -> OpportunityRepository:
        repo: OpportunityRepository
        if request.param == "sqlite":
            repo = SqlRepository.sqlite(data_dir / "opportunities.db")
        else:
            repo = ExcelRepository(data_dir / "opportunities.xlsx")
        opened.append(repo)
        return repo

    yield factory
    for repo in opened:
        repo.close()


def _mock(bodies: dict[SourceName, Any]) -> None:
    for name, (url, fixture) in ENDPOINTS.items():
        body = bodies.get(name, load_source_fixture(fixture)["body"])
        respx.get(url).respond(json=body)  # no ETag: every run gets full content


async def _run(config: AppConfig, repo: OpportunityRepository, now: datetime) -> Any:
    sources = implemented_sources()
    clock = FakeClock(now)
    with StateStore(config.state_db_path) as store:
        async with create_async_client("me@example.com") as client:
            http = HttpClient(client, store, clock=clock, sleep=clock.sleep)
            fetched = [
                await fetch_source(config, name, sources[name], http, store, now)
                for name in ENDPOINTS
            ]
    assert all(f.fatal is None and f.report.complete for f in fetched), fetched
    raw = [job for f in fetched for job in f.jobs]
    result = ingest(raw, repo, preferred_countries=config.settings.target_countries, now=now)
    repo.flush()
    return result


@respx.mock
async def test_two_consecutive_runs_add_no_duplicates(
    app_config: AppConfig, repo_factory: Any
) -> None:
    _mock({})
    repo = repo_factory()
    first = await _run(app_config, repo, T0)
    assert first.errors == []
    assert len(first.new) == 12  # 3 jobs from each of the four fixtures
    count = repo.count()
    repo.close()

    reopened = repo_factory()  # e.g. the next hourly run
    later = T0 + timedelta(hours=1)
    second = await _run(app_config, reopened, later)
    assert second.new == []
    assert second.by_rule == {MatchRule.SOURCE_KEY: 12}
    assert reopened.count() == count
    assert all(o.last_seen_at == later for o in reopened.iter_active())


@respx.mock
@pytest.mark.parametrize("aggregator_first", [True, False])
async def test_cross_source_merge_keeps_ats_apply_link(
    app_config: AppConfig, repo_factory: Any, aggregator_first: bool
) -> None:
    """The same GitLab job on Remotive and on GitLab's Greenhouse board becomes one
    opportunity whose canonical record (and apply link) is the ATS one."""
    gitlab = load_source_fixture("greenhouse_gitlab")["body"]
    ats_job = gitlab["jobs"][0]  # "Account Executive - France", "Remote, France"
    remotive = copy.deepcopy(load_source_fixture("remotive_software_dev")["body"])
    remotive["jobs"][0].update(
        id=999001,
        company_name="GitLab Inc.",
        title="Account Executive - France",
        candidate_required_location="France",
        url="https://remotive.com/remote-jobs/sales/account-executive-france-999001",
    )
    repo = repo_factory()
    if aggregator_first:
        _mock(
            {
                SourceName.GREENHOUSE: {"jobs": [], "meta": {"total": 0}},
                SourceName.REMOTIVE: remotive,
            }
        )
        await _run(app_config, repo, T0)
        respx.reset()
        _mock({SourceName.REMOTIVE: remotive})
        result = await _run(app_config, repo, T0 + timedelta(hours=1))
    else:
        _mock({SourceName.REMOTIVE: remotive})
        result = await _run(app_config, repo, T0)

    merged = repo.get_by_source_key("greenhouse", str(ats_job["id"]))
    assert merged is not None
    assert repo.get_by_source_key("remotive", "999001") == merged
    assert merged.source == "greenhouse"
    assert merged.apply_url == ats_job["absolute_url"]
    assert merged.posting_url == ats_job["absolute_url"]
    (sighting,) = repo.sightings_for(merged.opportunity_id)
    assert sighting.source == "remotive"
    assert sighting.posting_url.startswith("https://remotive.com/")  # attribution kept
    assert MatchRule.FINGERPRINT in result.by_rule
    assert sum(1 for o in repo.iter_active() if o.title == "Account Executive - France") == 1
