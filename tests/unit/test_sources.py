"""Parsers against recorded responses, and per-source fetch behaviour."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobfinder.config import AtsCompany
from jobfinder.http.client import (
    DailyCapReachedError,
    HttpClient,
    RequestPolicy,
    create_async_client,
)
from jobfinder.models import EmploymentType, RemoteType
from jobfinder.pipeline.normalise import normalise
from jobfinder.sources.ashby import AshbySource
from jobfinder.sources.base import FetchContext, FetchReport
from jobfinder.sources.greenhouse import GreenhouseSource
from jobfinder.sources.lever import LeverSource
from jobfinder.sources.remotive import RemotiveSource, parse_response
from jobfinder.storage.state_store import SourceState, StateStore
from tests.helpers import FakeSleep, load_source_fixture

GITLAB = AtsCompany(name="GitLab Config Name", provider="greenhouse", board_token="gitlab")
PALANTIR = AtsCompany(name="Palantir", provider="lever", board_token="palantir")
RAMP = AtsCompany(name="Ramp", provider="ashby", board_token="ramp")
T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


# --------------------------------------------------------------------------- parsers


def test_greenhouse_parser() -> None:
    report = FetchReport()
    jobs = GreenhouseSource().parse_board(
        load_source_fixture("greenhouse_gitlab")["body"], GITLAB, report
    )
    assert len(jobs) == 3
    first = jobs[0]
    assert (first.source, first.source_job_id, first.is_ats) == ("greenhouse", "8860302002", True)
    assert first.title == "Account Executive - France"
    assert first.company == "GitLab"  # the API's company_name wins over the config name
    assert first.locations == ["Remote, France"]
    assert first.posting_url == "https://job-boards.greenhouse.io/gitlab/jobs/8860302002"
    assert first.apply_url == first.posting_url
    assert first.published_at == datetime(2026, 10, 2, 15, 31, 50, tzinfo=UTC)
    assert first.description_html is not None and first.description_html.startswith("&lt;")
    job = normalise(first, ["FR"])
    assert job.remote_type is RemoteType.REMOTE
    assert job.location_country == "FR"
    assert "GitLab is the intelligent orchestration platform" in job.description
    assert "&lt;" not in job.description and "<div" not in job.description
    multi = normalise(jobs[1], ["DE"])
    assert multi.location_country == "DE"
    assert {"FR", "DE", "IE", "NL", "GB"} <= multi.countries
    bangalore = normalise(jobs[2], ["FR"])
    assert (bangalore.location_city, bangalore.location_country) == ("Bangalore", "IN")
    assert report.skipped_records == 0


def test_lever_parser() -> None:
    jobs = LeverSource().parse_board(
        load_source_fixture("lever_palantir")["body"], PALANTIR, FetchReport()
    )
    assert [j.title for j in jobs] == [
        "Administrative Business Partner",
        "Deployment Strategist, Internship",
        "Deployment Strategist",
    ]
    first = jobs[0]
    assert first.source_job_id == "6ed76ce8-4156-4b60-b120-403538bd66cd"
    assert first.company == "Palantir"
    assert first.country_hints == ["SG"]
    assert first.remote_hint == "hybrid"
    assert first.apply_url == first.posting_url + "/apply"
    assert first.published_at is not None and first.published_at.tzinfo is UTC
    intern = normalise(jobs[1], ["FR"])
    assert intern.employment_type is EmploymentType.INTERNSHIP
    assert (intern.location_city, intern.location_country) == ("Paris", "FR")
    assert intern.remote_type is RemoteType.ONSITE
    fixed = normalise(jobs[2], [])
    assert fixed.employment_type is EmploymentType.TEMPORARY
    assert fixed.location_country == "AE"
    assert normalise(first, []).remote_type is RemoteType.HYBRID


def test_lever_eu_region_url() -> None:
    eu = AtsCompany(name="X", provider="lever", board_token="x", region="eu")
    assert LeverSource().board_url(eu) == "https://api.eu.lever.co/v0/postings/x"


def test_ashby_parser() -> None:
    body = load_source_fixture("ashby_ramp")["body"]
    jobs = AshbySource().parse_board(body, RAMP, FetchReport())
    assert [j.title for j in jobs] == [
        "Security Engineer, Cloud",  # leading space in the API title is trimmed
        "Applied Scientist Intern",
        "Account Manager | Mid-Market",
    ]
    first = jobs[0]
    assert first.source_job_id == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert first.locations[0] == "New York, NY (HQ)"
    assert "USA" in first.country_hints
    assert first.apply_url is not None and first.apply_url.endswith("/application")
    assert normalise(first, []).remote_type is RemoteType.HYBRID
    intern = normalise(jobs[1], [])
    assert intern.employment_type is EmploymentType.INTERNSHIP
    assert (intern.location_city, intern.location_country) == ("New York", "US")
    assert normalise(jobs[2], []).remote_type is RemoteType.REMOTE


def test_ashby_without_id_uses_job_url() -> None:
    record = dict(load_source_fixture("ashby_ramp")["body"]["jobs"][0])
    del record["id"]
    job = AshbySource().parse_job(record, RAMP)
    assert job is not None
    assert job.source_job_id == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"


def test_ashby_skips_unlisted() -> None:
    record = dict(load_source_fixture("ashby_ramp")["body"]["jobs"][0], isListed=False)
    assert AshbySource().parse_job(record, RAMP) is None


def test_remotive_parser() -> None:
    jobs = parse_response(load_source_fixture("remotive_software_dev")["body"], FetchReport())
    assert len(jobs) == 3
    first = jobs[0]
    assert (first.source, first.source_job_id, first.is_ats) == ("remotive", "1680495", False)
    assert first.company == "Coalition Technologies"
    assert (
        first.posting_url
        == "https://remotive.com/remote-jobs/marketing/remote-office-assistant-1680495"
    )
    assert first.apply_url is None
    assert first.published_at == datetime(2026, 10, 11, 20, 17, 27, tzinfo=UTC)
    job = normalise(first, [])
    assert job.remote_type is RemoteType.REMOTE
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.location_country is None  # "Worldwide"


def test_malformed_records_are_skipped() -> None:
    body = load_source_fixture("remotive_software_dev")["body"]
    body = dict(body, jobs=[*body["jobs"], {"id": 1}, "garbage", {"title": None}])
    report = FetchReport()
    assert len(parse_response(body, report)) == 3


# --------------------------------------------------------------------------- fetch


@pytest.fixture
async def http(tmp_path: Path) -> AsyncIterator[HttpClient]:
    async with create_async_client("me@example.com") as client:
        yield HttpClient(
            client, StateStore(tmp_path / "state.db"), clock=lambda: T0, sleep=FakeSleep()
        )


def _ctx(http: HttpClient, source: str, **kw: object) -> FetchContext:
    return FetchContext(
        http=http,
        policy=RequestPolicy(source=source, daily_request_cap=50, requests_per_minute={}),
        now=T0,
        **kw,  # type: ignore[arg-type]
    )


@respx.mock
async def test_ats_board_failure_does_not_stop_other_boards(http: HttpClient) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs").respond(
        json=load_source_fixture("greenhouse_gitlab")["body"]
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/nope/jobs").respond(404)
    nope = AtsCompany(name="Nope", provider="greenhouse", board_token="nope")
    ctx = _ctx(http, "greenhouse", ats_companies=[GITLAB, nope, PALANTIR])
    jobs = await GreenhouseSource().fetch(ctx, SourceState("greenhouse"))
    assert len(jobs) == 3
    assert not ctx.report.complete
    assert ctx.report.errors == [
        "Nope: greenhouse: HTTP 404 from https://boards-api.greenhouse.io/v1/boards/nope/jobs"
        " (check board_token)"
    ]


@respx.mock
async def test_ats_not_modified_is_reported(http: HttpClient) -> None:
    route = respx.get("https://api.ashbyhq.com/posting-api/job-board/ramp")
    route.side_effect = [
        httpx.Response(
            200, json=load_source_fixture("ashby_ramp")["body"], headers={"ETag": '"1"'}
        ),
        httpx.Response(304),
    ]
    source = AshbySource()
    assert (
        len(await source.fetch(_ctx(http, "ashby", ats_companies=[RAMP]), SourceState("ashby")))
        == 3
    )
    ctx = _ctx(http, "ashby", ats_companies=[RAMP])
    assert await source.fetch(ctx, SourceState("ashby")) == []
    assert ctx.report.complete
    assert len(ctx.report.not_modified) == 1


@respx.mock
async def test_ats_daily_cap_propagates(http: HttpClient) -> None:
    respx.get(url__startswith="https://api.lever.co/").respond(json=[])
    ctx = FetchContext(
        http=http,
        policy=RequestPolicy(source="lever", daily_request_cap=1, requests_per_minute={}),
        now=T0,
        ats_companies=[PALANTIR, AtsCompany(name="B", provider="lever", board_token="b")],
    )
    with pytest.raises(DailyCapReachedError):
        await LeverSource().fetch(ctx, SourceState("lever"))


@respx.mock
async def test_remotive_queries_and_params(http: HttpClient) -> None:
    route = respx.get("https://remotive.com/api/remote-jobs").respond(
        json=load_source_fixture("remotive_software_dev")["body"]
    )
    ctx = _ctx(
        http,
        "remotive",
        queries=[{"category": "software-dev", "unsupported": "x"}, {"search": "python"}],
    )
    jobs = await RemotiveSource().fetch(ctx, SourceState("remotive"))
    assert len(jobs) == 6
    params = [dict(call.request.url.params) for call in route.calls]
    assert params == [{"category": "software-dev"}, {"search": "python"}]
