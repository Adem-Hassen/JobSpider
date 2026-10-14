"""robots.txt, caps beyond the daily one, per-source ceilings, scheduling, breaker."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobfinder.config import AppConfig, SourceName, load_config
from jobfinder.http.client import (
    CapReachedError,
    HttpClient,
    RequestPolicy,
    RobotsDisallowedError,
    create_async_client,
)
from jobfinder.http.identity import never_fetch
from jobfinder.http.robots import RobotsCache
from jobfinder.pipeline.fetch import FetchStatus, fetch_source, policy_for
from jobfinder.pipeline.schedule import due_sources, plan
from jobfinder.sources.base import FetchContext
from jobfinder.storage.state_store import SourceState, StateStore
from tests.helpers import FakeClock

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)
POLICY = RequestPolicy("demo", 1000, {})


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StateStore]:
    with StateStore(tmp_path / "state.db") as s:
        yield s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
async def http(store: StateStore, clock: FakeClock) -> AsyncIterator[HttpClient]:
    async with create_async_client("me@example.com") as client:
        yield HttpClient(client, store, clock=clock, sleep=clock.sleep)


# --------------------------------------------------------------------------- robots.txt


@respx.mock
async def test_robots_checked_for_non_api_requests(http: HttpClient, clock: FakeClock) -> None:
    robots = respx.get("https://careers.example.test/robots.txt").respond(
        text="User-agent: *\nDisallow: /private\n"
    )
    respx.get("https://careers.example.test/jobs/1").respond(json={})
    await http.get_json("https://careers.example.test/jobs/1", POLICY, documented_api=False)
    with pytest.raises(RobotsDisallowedError):
        await http.get_json("https://careers.example.test/private/1", POLICY, documented_api=False)
    assert robots.call_count == 1  # cached
    clock.now += timedelta(hours=25)
    await http.get_json("https://careers.example.test/jobs/1", POLICY, documented_api=False)
    assert robots.call_count == 2  # refreshed after 24 h


@respx.mock
async def test_documented_apis_skip_robots(http: HttpClient) -> None:
    robots = respx.get("https://api.example.test/robots.txt").respond(
        text="User-agent: *\nDisallow: /"
    )
    respx.get("https://api.example.test/v1/jobs").respond(json=[])
    await http.get_json("https://api.example.test/v1/jobs", POLICY)
    assert robots.call_count == 0


@pytest.mark.parametrize(
    ("status", "body", "allowed"),
    [
        (404, "", True),
        (410, "", True),
        (401, "", False),
        (403, "", False),
        (503, "", False),
        (200, "User-agent: jobfinder\nDisallow: /\n\nUser-agent: *\nAllow: /", False),
        (200, "User-agent: *\nDisallow:", True),
    ],
)
async def test_robots_interpretation(
    store: StateStore, status: int, body: str, allowed: bool
) -> None:
    async def fetch(url: str) -> tuple[int, str]:
        return status, body

    cache = RobotsCache(store, fetch, lambda: T0)
    assert await cache.allowed("https://x.test/jobs/1") is allowed


async def test_robots_network_error_disallows_and_is_not_cached(store: StateStore) -> None:
    calls = 0

    async def fetch(url: str) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("down")
        return 404, ""

    cache = RobotsCache(store, fetch, lambda: T0)
    assert not await cache.allowed("https://x.test/a")
    assert await cache.allowed("https://x.test/a")


@pytest.mark.parametrize(
    ("host", "blocked"),
    [
        ("www.linkedin.com", True),
        ("fr.indeed.com", True),
        ("uk.indeed.com", True),
        ("www.glassdoor.co.uk", True),
        ("indeed.fr", True),
        ("boards.greenhouse.io", False),
        ("notlinkedin.example", False),
    ],
)
def test_never_fetch(host: str, blocked: bool) -> None:
    assert never_fetch(host) is blocked


# --------------------------------------------------------------------------- caps


@respx.mock
async def test_weekly_and_monthly_caps(http: HttpClient, store: StateStore) -> None:
    respx.get("https://api.example.test/x").respond(json={})
    policy = RequestPolicy("adz", 100, {}, weekly_cap=10, monthly_cap=20)
    for back in range(1, 7):  # 9 requests in the previous 6 days
        store.increment_counter(
            "source:adz", date(2026, 10, 14) - timedelta(days=back), 9 if back == 1 else 0
        )
    await http.get_json("https://api.example.test/x", policy)  # 10th in the 7-day window
    with pytest.raises(CapReachedError, match="7-day request cap"):
        await http.get_json("https://api.example.test/x", policy)
    store.increment_counter("source:adz", date(2026, 9, 20), 10)  # older than 7 days
    store.increment_counter("source:adz", date(2026, 10, 1), 0)
    assert store.sum_counter("source:adz", date(2026, 9, 15), date(2026, 10, 14)) == 20


@respx.mock
async def test_monthly_cap_blocks(http: HttpClient, store: StateStore) -> None:
    respx.get("https://api.example.test/x").respond(json={})
    store.increment_counter("source:adz", date(2026, 9, 20), 2500)
    policy = RequestPolicy("adz", 100, {}, weekly_cap=1000, monthly_cap=2500)
    with pytest.raises(CapReachedError, match="30-day request cap"):
        await http.get_json("https://api.example.test/x", policy)
    assert respx.calls.call_count == 0


def test_terms_ceiling_applies_to_unlisted_hosts(app_config: AppConfig) -> None:
    remotive = policy_for(app_config, SourceName.REMOTIVE)
    assert remotive.per_minute("remotive.com") == 2
    assert remotive.per_minute("cdn.remotive.com") == 2  # unlisted host: 30/min default, capped
    adzuna = policy_for(app_config, SourceName.ADZUNA)
    assert (adzuna.weekly_cap, adzuna.monthly_cap) == (1000, 2500)
    assert policy_for(app_config, SourceName.GREENHOUSE).per_minute("x.test") == 30


@respx.mock
async def test_post_form_and_204(http: HttpClient) -> None:
    respx.post("https://auth.example.test/token").respond(json={"access_token": "t"})
    respx.get("https://api.example.test/empty").respond(204)
    token = await http.post_form("https://auth.example.test/token", POLICY, {"a": "b"})
    assert token.data == {"access_token": "t"}
    empty = await http.get_json("https://api.example.test/empty", POLICY)
    assert (empty.status_code, empty.data) == (204, None)


# --------------------------------------------------------------------------- schedule


@pytest.fixture
def config(base_env: dict[str, str], edit_yaml: Any) -> AppConfig:
    def enable(d: dict[str, Any]) -> None:
        for name in ("remoteok", "jobicy", "himalayas", "arbeitnow"):
            d["sources"][name]["enabled"] = True

    edit_yaml("sources.yaml", enable)
    return load_config(env_file=None)


def test_plan_reasons(config: AppConfig, store: StateStore) -> None:
    implemented = set(SourceName) - {SourceName.EMAIL_ALERTS}
    store.save_source_state(SourceState("remotive", last_attempt_at=T0 - timedelta(hours=1)))
    store.save_source_state(
        SourceState("lever", paused_until=T0 + timedelta(hours=3), pause_reason="401")
    )
    store.increment_counter("source:ashby", T0.date(), 200)
    reasons = {d.name.value: d.reason for d in plan(config, store, implemented, T0)}
    assert reasons["greenhouse"] == "due"
    assert reasons["remotive"].startswith("next due 17:00")
    assert reasons["lever"].startswith("paused until") and "401" in reasons["lever"]
    assert reasons["ashby"] == "daily request cap used up"
    assert reasons["adzuna"] == "disabled"
    assert set(due_sources(config, store, implemented, T0)) == {
        SourceName.GREENHOUSE,
        SourceName.REMOTEOK,
        SourceName.JOBICY,
        SourceName.HIMALAYAS,
        SourceName.ARBEITNOW,
    }


def test_interval_slack_never_breaks_terms(config: AppConfig, store: StateStore) -> None:
    def jobicy_hourly(d: dict[str, Any]) -> None:
        d["sources"]["jobicy"]["min_interval_minutes"] = 60

    store.save_source_state(SourceState("jobicy", last_attempt_at=T0))
    store.save_source_state(SourceState("greenhouse", last_attempt_at=T0))
    implemented = set(SourceName)
    at_57 = T0 + timedelta(minutes=57)
    hourly = config.sources.model_copy(deep=True)
    hourly.sources[SourceName.JOBICY] = hourly.sources[SourceName.JOBICY].model_copy(
        update={"min_interval_minutes": 60}
    )
    hourly.sources[SourceName.GREENHOUSE] = hourly.sources[SourceName.GREENHOUSE].model_copy(
        update={"min_interval_minutes": 60}
    )
    from dataclasses import replace

    cfg = replace(config, sources=hourly)
    due = due_sources(cfg, store, implemented, at_57)
    assert SourceName.GREENHOUSE in due  # 5-minute slack for timer jitter
    assert SourceName.JOBICY not in due  # Jobicy's terms: at most hourly, no slack


# --------------------------------------------------------------------------- breaker


class Failing:
    name = "arbeitnow"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[Any]:
        raise self.error


async def test_three_failed_runs_pause_for_24h(
    config: AppConfig, http: HttpClient, store: StateStore
) -> None:
    source = Failing(RuntimeError("boom"))
    for hour in range(3):
        now = T0 + timedelta(hours=3 * hour)
        result = await fetch_source(config, SourceName.ARBEITNOW, source, http, store, now)
        assert result.status is FetchStatus.FAILED
    state = store.get_source_state("arbeitnow")
    assert state.paused_until == T0 + timedelta(hours=6, days=1)
    assert "3 failed runs in a row" in (state.pause_reason or "")
    assert result.alerts and "paused for 24 h" in result.alerts[0]


async def test_success_resets_failures(
    config: AppConfig, http: HttpClient, store: StateStore
) -> None:
    await fetch_source(config, SourceName.ARBEITNOW, Failing(RuntimeError("x")), http, store, T0)
    assert store.get_source_state("arbeitnow").consecutive_failures == 1

    class Ok:
        name = "arbeitnow"

        async def fetch(self, ctx: FetchContext, state: SourceState) -> list[Any]:
            return []

    result = await fetch_source(config, SourceName.ARBEITNOW, Ok(), http, store, T0)
    assert result.status is FetchStatus.OK
    state = store.get_source_state("arbeitnow")
    assert (state.consecutive_failures, state.last_success_at) == (0, T0)


async def test_cap_is_not_a_failure(config: AppConfig, http: HttpClient, store: StateStore) -> None:
    result = await fetch_source(
        config, SourceName.ARBEITNOW, Failing(CapReachedError("cap")), http, store, T0
    )
    assert result.status is FetchStatus.CAPPED
    assert store.get_source_state("arbeitnow").consecutive_failures == 0


async def test_dry_run_records_nothing(
    config: AppConfig, http: HttpClient, store: StateStore
) -> None:
    await fetch_source(
        config, SourceName.ARBEITNOW, Failing(RuntimeError("x")), http, store, T0, record=False
    )
    assert store.get_source_state("arbeitnow") == SourceState("arbeitnow")
