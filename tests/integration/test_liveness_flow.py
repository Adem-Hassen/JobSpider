"""Full-feed expiry through the real fetch -> ingest -> expire path, both backends."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from jobfinder.config import AppConfig, SourceName
from jobfinder.http.client import HttpClient, create_async_client
from jobfinder.models import ListingStatus
from jobfinder.pipeline.fetch import fetch_source
from jobfinder.pipeline.ingest import ingest
from jobfinder.pipeline.liveness import expire_missing
from jobfinder.sources.registry import implemented_sources
from jobfinder.storage.excel_repo import ExcelRepository
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.sql_repo import SqlRepository
from jobfinder.storage.state_store import StateStore
from tests.helpers import FakeClock, load_source_fixture

T0 = datetime(2026, 10, 14, 9, tzinfo=UTC)
BOARD = "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs"


@pytest.fixture(params=["sqlite", "excel"])
def repo(request: pytest.FixtureRequest, app_config: AppConfig) -> Iterator[OpportunityRepository]:
    r: OpportunityRepository = (
        SqlRepository.sqlite(app_config.data_dir / "o.db")
        if request.param == "sqlite"
        else ExcelRepository(app_config.data_dir / "o.xlsx")
    )
    yield r
    r.close()


async def run(
    config: AppConfig, repo: OpportunityRepository, response: httpx.Response, now: datetime
) -> list[Any]:
    respx.get(BOARD).mock(return_value=response)
    clock = FakeClock(now)
    with StateStore(config.state_db_path) as store:
        async with create_async_client("me@example.com") as client:
            http = HttpClient(client, store, clock=clock, sleep=clock.sleep)
            fetched = await fetch_source(
                config,
                SourceName.GREENHOUSE,
                implemented_sources()[SourceName.GREENHOUSE],
                http,
                store,
                now,
            )
        ingest(fetched.jobs, repo, preferred_countries=["FR"], now=now)
        expired = expire_missing(repo, store, fetched, now)
    repo.flush()
    return expired


def status_of(repo: OpportunityRepository, job_id: str) -> ListingStatus:
    found = repo.get_by_source_key("greenhouse", job_id)
    assert found is not None
    return found.listing_status


@respx.mock
async def test_job_missing_from_two_complete_fetches_expires(
    app_config: AppConfig, repo: OpportunityRepository
) -> None:
    full = load_source_fixture("greenhouse_gitlab")["body"]
    gone_id = str(full["jobs"][1]["id"])
    without = copy.deepcopy(full)
    del without["jobs"][1]

    await run(app_config, repo, httpx.Response(200, json=full), T0)
    assert (
        await run(app_config, repo, httpx.Response(200, json=without), T0 + timedelta(hours=6))
        == []
    )
    assert status_of(repo, gone_id) is ListingStatus.ACTIVE  # one miss is not enough
    # A 304 is neither a sighting nor an absence.
    await run(app_config, repo, httpx.Response(304), T0 + timedelta(hours=12))
    assert status_of(repo, gone_id) is ListingStatus.ACTIVE
    expired = await run(
        app_config, repo, httpx.Response(200, json=without), T0 + timedelta(hours=18)
    )
    assert [o.source_job_id for o in expired] == [gone_id]
    assert status_of(repo, gone_id) is ListingStatus.EXPIRED
    assert len(list(repo.iter_active())) == 2

    # If the employer reopens it, the next fetch makes it active again.
    await run(app_config, repo, httpx.Response(200, json=full), T0 + timedelta(hours=24))
    assert status_of(repo, gone_id) is ListingStatus.ACTIVE


@respx.mock
async def test_failed_fetch_never_expires(
    app_config: AppConfig, repo: OpportunityRepository
) -> None:
    full = load_source_fixture("greenhouse_gitlab")["body"]
    await run(app_config, repo, httpx.Response(200, json=full), T0)
    for hour in (6, 12, 18):
        await run(app_config, repo, httpx.Response(404), T0 + timedelta(hours=hour))
    assert len(list(repo.iter_active())) == 3
