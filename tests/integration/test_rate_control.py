"""Scheduling and rate control over simulated hourly runs, all sources mocked."""

from __future__ import annotations

import itertools
import random
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from jobfinder.config import SOURCE_TERMS, AppConfig, SourceName, load_config
from jobfinder.http.client import HttpClient, create_async_client
from jobfinder.pipeline.fetch import FetchStatus, SourceFetch, fetch_source
from jobfinder.pipeline.schedule import due_sources
from jobfinder.sources import france_travail
from jobfinder.sources.registry import implemented_sources
from jobfinder.storage.state_store import StateStore
from tests.helpers import FakeClock, load_source_fixture

START = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)  # 24 ticks cross UTC midnight
HTTP_SOURCES = [s for s in SourceName if s is not SourceName.EMAIL_ALERTS]


@pytest.fixture
def config(base_env: dict[str, str], edit_yaml: Any, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    for var, value in {
        "ADZUNA_APP_ID": "id",
        "ADZUNA_APP_KEY": "key",
        "FRANCE_TRAVAIL_CLIENT_ID": "cid",
        "FRANCE_TRAVAIL_CLIENT_SECRET": "secret",
    }.items():
        monkeypatch.setenv(var, value)

    def enable_all(d: dict[str, Any]) -> None:
        for name in HTTP_SOURCES:
            d["sources"][name.value]["enabled"] = True
        # Make caps, not intervals, the binding limit for some sources.
        d["sources"]["arbeitnow"].update(min_interval_minutes=60, daily_request_cap=5)
        d["sources"]["jobicy"].update(min_interval_minutes=60, queries=[{"geo": "emea"}])
        d["sources"]["adzuna"].update(
            min_interval_minutes=60,
            queries=[
                {"country": "fr", "what": "python", "results_per_page": 2},
                {"country": "de", "what": "python", "results_per_page": 2},
            ],
        )

    edit_yaml("sources.yaml", enable_all)
    return load_config(env_file=None)


class Recorder:
    """Serves fixtures for every endpoint and records (source host, time) per request."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: dict[str, list[datetime]] = defaultdict(list)

    def serve(
        self, body: Any, status: int = 200, headers: dict[str, str] | None = None
    ) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls[request.url.host].append(self.clock.now)
            return httpx.Response(status, json=body, headers=headers or {})

        return handler

    def mock_all(self) -> None:
        fixture = lambda name: load_source_fixture(name)["body"]  # noqa: E731
        routes = {
            r"https://boards-api\.greenhouse\.io/.*": fixture("greenhouse_gitlab"),
            r"https://api\.lever\.co/.*": fixture("lever_palantir"),
            r"https://api\.ashbyhq\.com/.*": fixture("ashby_ramp"),
            r"https://remotive\.com/.*": fixture("remotive_software_dev"),
            r"https://remoteok\.com/.*": fixture("remoteok"),
            r"https://jobicy\.com/.*": fixture("jobicy_page1"),
            r"https://himalayas\.app/.*": fixture("himalayas_page1"),
            r"https://www\.arbeitnow\.com/.*": fixture("arbeitnow_page1"),
            r"https://api\.adzuna\.com/.*": fixture("adzuna_fr_search"),
            r"https://api\.francetravail\.io/.*": fixture("france_travail_search"),
        }
        for pattern, body in routes.items():
            respx.get(url__regex=pattern).mock(side_effect=self.serve(body))
        respx.post(france_travail.OAUTH_ENDPOINT).mock(
            side_effect=self.serve(load_source_fixture("france_travail_token")["body"])
        )


async def tick(
    config: AppConfig, store: StateStore, clock: FakeClock, now: datetime
) -> list[SourceFetch]:
    clock.now = max(clock.now, now)
    sources = implemented_sources()
    results = []
    async with create_async_client("me@example.com") as client:
        http = HttpClient(client, store, clock=clock, sleep=clock.sleep)
        for name in due_sources(config, store, sources, clock.now):
            results.append(await fetch_source(config, name, sources[name], http, store, clock.now))
    return results


@respx.mock
async def test_24_hourly_ticks_never_exceed_any_cap(config: AppConfig) -> None:
    clock = FakeClock(START)
    recorder = Recorder(clock)
    recorder.mock_all()
    jitter = random.Random(7)  # noqa: S311 - reproducible test jitter, not security
    attempts: dict[str, list[datetime]] = defaultdict(list)
    with StateStore(config.state_db_path) as store:
        for hour in range(24):
            now = START + timedelta(hours=hour, seconds=jitter.randint(0, 300))
            for result in await tick(config, store, clock, now):
                attempts[result.name.value].append(now)
                assert result.status in (FetchStatus.OK, FetchStatus.PARTIAL, FetchStatus.CAPPED), (
                    result
                )

        # Daily caps per UTC day, as recorded by the counters.
        for name in HTTP_SOURCES:
            cap = config.sources.sources[name].daily_request_cap
            for day in (START.date(), (START + timedelta(days=1)).date()):
                used = store.get_counter(f"source:{name.value}", day)
                assert used <= cap, (name, day, used, cap)
        assert store.get_counter("source:arbeitnow", START.date()) == 5  # the cap did bind

    # Remotive: at most 4 a day and 2 a minute.
    remotive = recorder.calls["remotive.com"]
    assert 1 <= len(remotive) <= 8  # two UTC days
    for day in {t.date() for t in remotive}:
        assert sum(1 for t in remotive if t.date() == day) <= 4

    # Jobicy: runs at least an hour apart.
    jobicy_runs = attempts["jobicy"]
    assert all(b - a >= timedelta(minutes=60) for a, b in itertools.pairwise(jobicy_runs))

    # Every host stays within its per-minute limit over any 60-second window.
    limits = {
        "remotive.com": 2,
        "api.adzuna.com": 25,
        "www.arbeitnow.com": 10,
        "jobicy.com": 2,
        "himalayas.app": 10,
        "remoteok.com": 1,
        "boards-api.greenhouse.io": 30,
    }
    for host, limit in limits.items():
        times = sorted(recorder.calls[host])
        for i, t in enumerate(times):
            window = [u for u in times[i:] if u - t < timedelta(seconds=60)]
            assert len(window) <= limit, (host, t, len(window))

    # Every source ran at least once, and none ran more often than its interval allows.
    for name in HTTP_SOURCES:
        runs = attempts[name.value]
        assert runs, name
        interval = timedelta(minutes=config.sources.sources[name].min_interval_minutes - 5)
        terms = SOURCE_TERMS.get(name)
        if terms and terms.min_interval_minutes:
            interval = max(interval, timedelta(minutes=terms.min_interval_minutes))
        assert all(b - a >= interval for a, b in itertools.pairwise(runs)), name


@respx.mock
async def test_429_retry_after_is_honoured_by_a_source(config: AppConfig) -> None:
    clock = FakeClock(START)
    respx.get("https://remotive.com/api/remote-jobs").side_effect = [
        httpx.Response(429, headers={"Retry-After": "20"}),
        httpx.Response(200, json=load_source_fixture("remotive_software_dev")["body"]),
    ]
    with StateStore(config.state_db_path) as store:
        async with create_async_client("me@example.com") as client:
            http = HttpClient(client, store, clock=clock, sleep=clock.sleep)
            result = await fetch_source(
                config,
                SourceName.REMOTIVE,
                implemented_sources()[SourceName.REMOTIVE],
                http,
                store,
                START,
            )
        assert result.status is FetchStatus.OK
        assert len(result.jobs) == 3
        assert 20.0 in clock.sleeps
        assert store.get_counter("source:remotive", START.date()) == 2  # the retry counted


@respx.mock
async def test_source_failing_three_runs_is_paused_then_resumes(config: AppConfig) -> None:
    clock = FakeClock(START)
    # Jobicy: hourly in this config and its daily cap (24) leaves room for retries,
    # so every run is a genuine failure rather than a capped run.
    failing = respx.get(url__startswith="https://jobicy.com/").respond(503)
    with StateStore(config.state_db_path) as store:
        alerts: list[str] = []
        runs = 0
        for hour in range(30):
            for result in await tick(config, store, clock, START + timedelta(hours=hour)):
                if result.name is SourceName.JOBICY:
                    runs += 1
                    alerts.extend(result.alerts)
            if hour == 2:
                state = store.get_source_state("jobicy")
                assert state.paused_until == START + timedelta(hours=2 + 24)
        # Runs at hours 0-2 fail -> paused until hour 26; runs at 26-28 fail -> paused again.
        assert runs == 6
        assert len(alerts) == 2 and all("3 failed runs in a row" in a for a in alerts)
        assert failing.call_count == 6 * 3  # each run: one request, retried twice


@respx.mock
async def test_401_pauses_immediately_with_alert(config: AppConfig) -> None:
    clock = FakeClock(START)
    respx.get(url__startswith="https://api.adzuna.com/").respond(401)
    with StateStore(config.state_db_path) as store:
        results = await tick(config, store, clock, START)
        (adzuna,) = [r for r in results if r.name is SourceName.ADZUNA]
        assert adzuna.status is FetchStatus.AUTH_FAILED
        assert "Check its credentials" in adzuna.alerts[0]
        assert "key" not in adzuna.alerts[0]  # no secrets in alerts
        later = await tick(config, store, clock, START + timedelta(hours=1))
        assert SourceName.ADZUNA not in {r.name for r in later}
