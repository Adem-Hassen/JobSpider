from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobfinder.config import LinkSettings
from jobfinder.http.client import HttpClient, create_async_client
from jobfinder.http.links import (
    MAX_HOPS,
    LinkResolver,
    follow,
    is_aggregator,
    links_policy,
    liveness_check,
    resolution_check,
    unsafe_reason,
)
from jobfinder.storage.state_store import StateStore
from tests.helpers import FakeClock

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)
POLICY = links_policy(LinkSettings(requests_per_minute=60))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StateStore]:
    with StateStore(tmp_path / "state.db") as s:
        yield s


@pytest.fixture
async def http(store: StateStore) -> AsyncIterator[HttpClient]:
    clock = FakeClock(T0)
    async with create_async_client("me@example.com") as client:
        yield HttpClient(client, store, clock=clock, sleep=clock.sleep)


def no_robots() -> None:
    respx.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://jobs.lever.co/acme/1", None),
        ("http://careers.acme.com/job/1", None),
        ("ftp://acme.com/job", "scheme ftp not allowed"),
        ("javascript:alert(1)", "scheme javascript not allowed"),
        ("https://user:pw@acme.com/job", "credentials in URL"),
        ("http://169.254.169.254/latest/meta-data", "IP address hosts are not followed"),
        ("http://[::1]/x", "IP address hosts are not followed"),
        ("http://10.0.0.5/x", "IP address hosts are not followed"),
        ("http://localhost/x", "not a public host name"),
        ("http://intranet/x", "not a public host name"),
        ("http://jobs.acme.internal/x", "local host name"),
        ("https://acme.com:8443/x", "non-standard port 8443"),
        ("https://www.linkedin.com/jobs/view/1", "site forbids automated access"),
        ("https://de.indeed.com/viewjob?jk=1", "site forbids automated access"),
        ("https://www.glassdoor.fr/job-listing/x", "site forbids automated access"),
        ("not a url", "scheme (none) not allowed"),
    ],
)
def test_unsafe_reason(url: str, reason: str | None) -> None:
    assert unsafe_reason(url) == reason


@pytest.mark.parametrize(
    ("url", "aggregator"),
    [
        ("https://www.adzuna.fr/land/ad/1", True),
        ("https://remotive.com/remote-jobs/x", True),
        ("https://candidat.francetravail.fr/offres/1", True),
        ("https://jobs.lever.co/a/1", False),
        ("https://careers.adzunaconsulting.com/1", False),
    ],
)
def test_is_aggregator(url: str, aggregator: bool) -> None:
    assert is_aggregator(url) is aggregator


def test_liveness_may_check_aggregator_pages_but_not_adzuna() -> None:
    assert resolution_check("https://remotive.com/remote-jobs/x-1") == "aggregator link"
    assert liveness_check("https://remotive.com/remote-jobs/x-1") is None
    assert liveness_check("https://www.adzuna.de/land/ad/1") is not None


@respx.mock
async def test_follow_redirect_chain(http: HttpClient) -> None:
    no_robots()
    respx.head("https://acme.com/apply/1").respond(301, headers={"Location": "/apply/1/"})
    respx.head("https://acme.com/apply/1/").respond(
        302, headers={"Location": "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/R1"}
    )
    respx.head("https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/R1").respond(200)
    walk = await follow(http, "https://acme.com/apply/1", POLICY, resolution_check)
    assert walk.final_url == "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/R1"
    assert (walk.status, walk.hops) == (200, 2)


@respx.mock
async def test_head_refused_falls_back_to_streamed_get(http: HttpClient) -> None:
    no_robots()
    respx.head("https://acme.com/job/1").respond(405)
    get = respx.get("https://acme.com/job/1").respond(200, text="<html>" + "x" * 10_000)
    walk = await follow(http, "https://acme.com/job/1", POLICY, resolution_check)
    assert walk.status == 200
    assert get.call_count == 1
    assert http.requests_by_source == {"links": 2}  # both requests counted


@respx.mock
async def test_too_many_redirects(http: HttpClient) -> None:
    no_robots()
    for i in range(MAX_HOPS + 2):
        respx.head(f"https://acme.com/r{i}").respond(302, headers={"Location": f"/r{i + 1}"})
    walk = await follow(http, "https://acme.com/r0", POLICY, resolution_check)
    assert walk.final_url is None
    assert walk.note == f"more than {MAX_HOPS} redirects"
    assert sum(1 for c in respx.calls if c.request.method == "HEAD") == MAX_HOPS + 1


@respx.mock
@pytest.mark.parametrize(
    "target",
    ["https://www.linkedin.com/jobs/view/9", "http://127.0.0.1/admin", "https://www.adzuna.fr/x"],
)
async def test_redirect_into_forbidden_host_is_not_followed(http: HttpClient, target: str) -> None:
    no_robots()
    respx.head("https://acme.com/job").respond(302, headers={"Location": target})
    walk = await follow(http, "https://acme.com/job", POLICY, resolution_check)
    assert walk.status is None
    assert walk.final_url == target  # best-known destination, never requested
    assert walk.note is not None and walk.note.startswith("stopped:")
    assert [c.request.url.host for c in respx.calls if c.request.method == "HEAD"] == ["acme.com"]


@respx.mock
async def test_robots_checked_on_every_hop(http: HttpClient) -> None:
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://ats.example.com/robots.txt").respond(text="User-agent: *\nDisallow: /")
    respx.head("https://acme.com/job").respond(
        302, headers={"Location": "https://ats.example.com/j/1"}
    )
    walk = await follow(http, "https://acme.com/job", POLICY, resolution_check)
    assert (walk.final_url, walk.status, walk.note) == (
        "https://ats.example.com/j/1",
        None,
        "robots.txt disallows",
    )


@respx.mock
async def test_resolver_caches_permanently(http: HttpClient, store: StateStore) -> None:
    no_robots()
    route = respx.head("https://jobs.lever.co/acme/1/apply").respond(200)
    resolver = LinkResolver(http, store, POLICY, lambda: T0)
    first = await resolver.resolve("https://jobs.lever.co/acme/1/apply")
    second = await resolver.resolve("https://jobs.lever.co/acme/1/apply")
    assert first == second
    assert first is not None and first.final_url == "https://jobs.lever.co/acme/1/apply"
    assert route.call_count == 1


@respx.mock
async def test_resolver_does_not_cache_errors(http: HttpClient, store: StateStore) -> None:
    no_robots()
    route = respx.head("https://acme.com/job")
    route.side_effect = [httpx.Response(404), httpx.Response(200)]
    resolver = LinkResolver(http, store, POLICY, lambda: T0)
    assert await resolver.resolve("https://acme.com/job") is None
    record = await resolver.resolve("https://acme.com/job")
    assert record is not None and record.status == 200


@respx.mock
async def test_resolver_never_requests_aggregators(http: HttpClient, store: StateStore) -> None:
    resolver = LinkResolver(http, store, POLICY, lambda: T0)
    record = await resolver.resolve("https://www.adzuna.fr/land/ad/1?se=x")
    assert record is not None
    assert (record.final_url, record.note) == (None, "not resolved: aggregator link")
    assert len(respx.calls) == 0
