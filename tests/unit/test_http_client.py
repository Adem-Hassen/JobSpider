from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobfinder.http.client import (
    DailyCapReachedError,
    FetchError,
    HttpClient,
    RateLimitedError,
    RequestPolicy,
    SourceAuthError,
    create_async_client,
    retry_after_seconds,
)
from jobfinder.storage.state_store import StateStore
from tests.helpers import FakeClock

URL = "https://api.example.test/jobs"
T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)
POLICY = RequestPolicy(source="demo", daily_request_cap=5, requests_per_minute={})


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
async def http(store: StateStore, clock: FakeClock) -> AsyncIterator[HttpClient]:
    async with create_async_client("me@example.com") as client:
        yield HttpClient(client, store, clock=clock, sleep=clock.sleep)


def _sleep_calls(http: HttpClient) -> list[float]:
    clock = http.clock
    assert isinstance(clock, FakeClock)
    return clock.sleeps


@respx.mock
async def test_identifies_itself(http: HttpClient) -> None:
    route = respx.get(URL).respond(json={"ok": True})
    result = await http.get_json(URL, POLICY, params={"q": "python"})
    assert result.data == {"ok": True}
    request = route.calls.last.request
    assert request.headers["User-Agent"].startswith("jobfinder/")
    assert request.headers["User-Agent"].endswith("(+mailto:me@example.com)")
    assert request.url.params["q"] == "python"


async def test_timeouts() -> None:
    async with create_async_client("me@example.com") as client:
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 30.0


@respx.mock
async def test_conditional_requests(http: HttpClient) -> None:
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, json={"n": 1}, headers={"ETag": 'W/"abc"', "Last-Modified": "x"}),
        httpx.Response(304),
    ]
    first = await http.get_json(URL, POLICY)
    second = await http.get_json(URL, POLICY)
    assert not first.not_modified
    assert second.not_modified
    assert second.data is None
    sent = route.calls[1].request.headers
    assert sent["If-None-Match"] == 'W/"abc"'
    assert sent["If-Modified-Since"] == "x"


@respx.mock
async def test_dry_run_neither_sends_nor_stores_validators(store: StateStore) -> None:
    route = respx.get(URL).respond(json={}, headers={"ETag": '"v1"'})
    async with create_async_client("me@example.com") as client:
        http = HttpClient(client, store, clock=lambda: T0, use_validators=False)
        await http.get_json(URL, POLICY)
        await http.get_json(URL, POLICY)
    assert "If-None-Match" not in route.calls[1].request.headers
    assert store.get_http_validators(URL) is None


@respx.mock
async def test_daily_cap(http: HttpClient, store: StateStore) -> None:
    respx.get(URL).respond(json={})
    policy = RequestPolicy(source="capped", daily_request_cap=2, requests_per_minute={})
    await http.get_json(URL, policy)
    await http.get_json(URL, policy)
    with pytest.raises(DailyCapReachedError, match="daily request cap"):
        await http.get_json(URL, policy)
    assert store.get_counter("source:capped", date(2026, 10, 14)) == 2
    assert respx.calls.call_count == 2
    assert http.requests_by_source == {"capped": 2}


@respx.mock
async def test_429_retry_after_honoured(http: HttpClient) -> None:
    respx.get(URL).side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"ok": 1}),
    ]
    result = await http.get_json(URL, POLICY)
    assert result.data == {"ok": 1}
    assert _sleep_calls(http) == [7.0]


@respx.mock
async def test_429_with_long_retry_after_is_not_waited_for(http: HttpClient) -> None:
    respx.get(URL).respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(RateLimitedError):
        await http.get_json(URL, POLICY)
    assert respx.calls.call_count == 1
    assert _sleep_calls(http) == []


@respx.mock
async def test_5xx_retried_three_times(http: HttpClient, store: StateStore) -> None:
    respx.get(URL).respond(503)
    with pytest.raises(FetchError, match="HTTP 503") as info:
        await http.get_json(URL, POLICY)
    assert info.value.status_code == 503
    assert respx.calls.call_count == 3
    backoffs = [s for s in _sleep_calls(http) if s > 0]
    assert 2 <= len(backoffs) <= 4  # two back-offs, plus any rate-limit spacing
    assert all(s <= 30 for s in backoffs)
    assert store.get_counter("source:demo", date(2026, 10, 14)) == 3  # retries count too


@respx.mock
async def test_timeout_retried_then_succeeds(http: HttpClient) -> None:
    respx.get(URL).side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=[])]
    assert (await http.get_json(URL, POLICY)).data == []


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 410, 422])
async def test_other_4xx_not_retried(http: HttpClient, status: int) -> None:
    respx.get(URL).respond(status)
    with pytest.raises(FetchError) as info:
        await http.get_json(URL, POLICY)
    assert info.value.status_code == status
    assert not isinstance(info.value, SourceAuthError)
    assert respx.calls.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors(http: HttpClient, status: int) -> None:
    respx.get(URL).respond(status)
    with pytest.raises(SourceAuthError):
        await http.get_json(URL, POLICY)
    assert respx.calls.call_count == 1


@respx.mock
async def test_invalid_json(http: HttpClient) -> None:
    respx.get(URL).respond(200, text="<html>maintenance</html>")
    with pytest.raises(FetchError, match="invalid JSON"):
        await http.get_json(URL, POLICY)


@respx.mock
async def test_per_host_rate_limit(http: HttpClient) -> None:
    respx.get(URL).respond(json={})
    policy = RequestPolicy("demo", 10, {"api.example.test": 2})
    await http.get_json(URL, policy)
    await http.get_json(URL, policy)
    assert _sleep_calls(http) == [pytest.approx(30.0)]


@respx.mock
async def test_retry_wait_also_refills_rate_limit(http: HttpClient) -> None:
    """After honouring Retry-After the host bucket has refilled: no extra wait."""
    respx.get(URL).side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={}),
    ]
    await http.get_json(URL, RequestPolicy("demo", 10, {"api.example.test": 30}))
    assert _sleep_calls(http) == [7.0]


def test_retry_after_formats() -> None:
    assert retry_after_seconds({"retry-after": "12"}, T0) == 12.0
    assert retry_after_seconds({"retry-after": "Wed, 14 Oct 2026 12:01:00 GMT"}, T0) == 60.0
    assert retry_after_seconds({"retry-after": "Tue, 13 Oct 2026 12:00:00 GMT"}, T0) == 0.0
    assert retry_after_seconds({"retry-after": "soon"}, T0) is None
    assert retry_after_seconds({}, T0) is None
