"""Shared HTTP access for all sources: identification, limits, caching, retries."""

from __future__ import annotations

import asyncio
import email.utils
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from jobfinder.http.identity import user_agent
from jobfinder.http.ratelimit import HostRateLimiter
from jobfinder.http.robots import RobotsCache
from jobfinder.models import utcnow
from jobfinder.storage.state_store import HttpValidators, StateStore

MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_S = 120.0
DEFAULT_PER_MINUTE = 30.0
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class FetchError(Exception):
    """A request failed for good; ``status_code`` is set for HTTP errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SourceAuthError(FetchError):
    """401/403: credentials or access revoked. The source is paused and an alert sent."""


class CapReachedError(FetchError):
    """A configured or terms-imposed request cap (day, week or month) is used up."""


class DailyCapReachedError(CapReachedError):
    """The source's daily request cap is used up."""


class RateLimitedError(FetchError):
    """429 with a Retry-After longer than we are willing to wait."""


class RobotsDisallowedError(FetchError):
    """robots.txt does not allow this URL."""


@dataclass(frozen=True)
class RequestPolicy:
    source: str
    daily_request_cap: int
    requests_per_minute: Mapping[str, float]
    max_per_minute: float | None = None  # ceiling from the source's terms, for every host
    weekly_cap: int | None = None  # rolling 7 days
    monthly_cap: int | None = None  # rolling 30 days

    def per_minute(self, host: str) -> float:
        rate = self.requests_per_minute.get(host, DEFAULT_PER_MINUTE)
        return min(rate, self.max_per_minute) if self.max_per_minute else rate


@dataclass(frozen=True)
class HttpResult:
    url: str
    status_code: int
    not_modified: bool
    data: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class _RetryableStatusError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def retry_after_seconds(headers: Mapping[str, str], now: datetime) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def create_async_client(contact_email: str, **kwargs: Any) -> httpx.AsyncClient:
    """The one shared httpx client: identifies us, sane timeouts, at most 4 connections."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(contact_email), "Accept": "application/json"},
        timeout=TIMEOUT,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        follow_redirects=True,
        **kwargs,
    )


@dataclass
class HttpClient:
    """Rate-limited, capped, cached, retrying requests on a shared httpx client.

    Every attempt (including retries) counts against the source's caps. Requests that
    are not documented API endpoints are checked against robots.txt first.
    """

    client: httpx.AsyncClient
    store: StateStore
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    max_total: int = 4
    max_retry_wait_s: float = MAX_RETRY_WAIT_S
    use_validators: bool = True  # False for dry runs, which must not affect later fetches
    # Sources with nothing stored (first run, or a restored workbook): fetch in full,
    # because a 304 would hide jobs that are not in storage.
    fresh_sources: set[str] = field(default_factory=set)
    requests_by_source: dict[str, int] = field(default_factory=dict)
    errors_by_source: dict[str, int] = field(default_factory=dict)
    _limiter: HostRateLimiter = field(init=False)
    _robots: RobotsCache = field(init=False)
    _total: asyncio.Semaphore = field(init=False)
    _hosts: dict[str, asyncio.Semaphore] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._limiter = HostRateLimiter(self.store, self.clock, self.sleep)
        self._robots = RobotsCache(self.store, self._fetch_robots, self.clock)
        self._total = asyncio.Semaphore(self.max_total)

    # ------------------------------------------------------------------ limits

    def reserve(self, policy: RequestPolicy) -> None:
        """Count one request against the source's caps, or raise if one is used up."""
        scope = f"source:{policy.source}"
        today = self.clock().astimezone(UTC).date()
        if self.store.get_counter(scope, today) >= policy.daily_request_cap:
            raise DailyCapReachedError(
                f"{policy.source}: daily request cap ({policy.daily_request_cap}) reached"
            )
        for cap, days, label in (
            (policy.weekly_cap, 7, "7-day"),
            (policy.monthly_cap, 30, "30-day"),
        ):
            if cap is not None:
                used = self.store.sum_counter(scope, today - timedelta(days=days - 1), today)
                if used >= cap:
                    raise CapReachedError(f"{policy.source}: {label} request cap ({cap}) reached")
        self.store.increment_counter(scope, today)
        self.requests_by_source[policy.source] = self.requests_by_source.get(policy.source, 0) + 1

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        return self._hosts.setdefault(host, asyncio.Semaphore(1))

    async def _fetch_robots(self, url: str) -> tuple[int, str]:
        host = httpx.URL(url).host
        async with self._semaphore(host), self._total:
            await self._limiter.acquire(host, DEFAULT_PER_MINUTE)
            response = await self.client.get(url, headers={"Accept": "text/plain"})
        return response.status_code, response.text

    # ------------------------------------------------------------------ requests

    async def _send(
        self,
        method: str,
        url: str,
        policy: RequestPolicy,
        *,
        params: Mapping[str, Any] | None,
        data: Mapping[str, str] | None,
        headers: Mapping[str, str],
        cache_key: str | None,
    ) -> httpx.Response:
        host = httpx.URL(url).host
        async with self._semaphore(host), self._total:
            self.reserve(policy)
            await self._limiter.acquire(host, policy.per_minute(host))
            sent = dict(headers)
            validators = (
                self.store.get_http_validators(cache_key)
                if cache_key is not None and self.use_validators
                else None
            )
            if validators is not None:
                if validators.etag:
                    sent["If-None-Match"] = validators.etag
                if validators.last_modified:
                    sent["If-Modified-Since"] = validators.last_modified
            response = await self.client.request(
                method, url, params=params, data=data, headers=sent
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableStatusError(response)
        return response

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatusError):
            requested = retry_after_seconds(exc.response.headers, self.clock())
            if requested is not None:
                return requested
        return float(wait_exponential_jitter(initial=1, max=30)(state))

    def _should_retry(self, exc: BaseException) -> bool:
        if isinstance(exc, _RetryableStatusError):
            requested = retry_after_seconds(exc.response.headers, self.clock())
            return requested is None or requested <= self.max_retry_wait_s
        return isinstance(exc, httpx.TimeoutException | httpx.TransportError)

    async def _request(
        self,
        method: str,
        url: str,
        policy: RequestPolicy,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        cache_key: str | None = None,
        documented_api: bool = True,
    ) -> httpx.Response:
        if not documented_api and not await self._robots.allowed(url):
            raise RobotsDisallowedError(f"{policy.source}: robots.txt disallows {url}")
        response: httpx.Response | None = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(MAX_ATTEMPTS),
                retry=retry_if_exception(self._should_retry),
                wait=self._wait,
                sleep=self.sleep,
                reraise=True,
            ):
                with attempt:
                    response = await self._send(
                        method,
                        url,
                        policy,
                        params=params,
                        data=data,
                        headers=headers or {},
                        cache_key=cache_key,
                    )
        except _RetryableStatusError as exc:
            self._count_error(policy)
            status = exc.response.status_code
            if status == 429:
                raise RateLimitedError(f"{policy.source}: HTTP 429 from {url}", 429) from exc
            raise FetchError(f"{policy.source}: HTTP {status} from {url}", status) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._count_error(policy)
            raise FetchError(f"{policy.source}: {type(exc).__name__} for {url}") from exc
        if response is None:  # pragma: no cover - AsyncRetrying always runs once
            raise FetchError(f"{policy.source}: no response from {url}")
        status = response.status_code
        if status in (401, 403):
            self._count_error(policy)
            raise SourceAuthError(f"{policy.source}: HTTP {status} from {url}", status)
        if status >= 400:
            self._count_error(policy)
            raise FetchError(f"{policy.source}: HTTP {status} from {url}", status)
        return response

    def _count_error(self, policy: RequestPolicy) -> None:
        self.errors_by_source[policy.source] = self.errors_by_source.get(policy.source, 0) + 1

    @staticmethod
    def _json(response: httpx.Response, policy: RequestPolicy, url: str) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(
                f"{policy.source}: invalid JSON from {url}", response.status_code
            ) from exc

    async def get_json(
        self,
        url: str,
        policy: RequestPolicy,
        *,
        params: Mapping[str, Any] | None = None,
        secret_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        conditional: bool = True,
        documented_api: bool = True,
    ) -> HttpResult:
        """GET returning parsed JSON (None for 204). ``secret_params`` (API keys) are sent
        but kept out of the conditional-request cache and of every error message."""
        cache_key = str(httpx.URL(url, params=params)) if params else url
        conditional = conditional and policy.source not in self.fresh_sources
        response = await self._request(
            "GET",
            url,
            policy,
            params={**(params or {}), **(secret_params or {})} or None,
            headers=headers,
            cache_key=cache_key if conditional else None,
            documented_api=documented_api,
        )
        if response.status_code == 304:
            return HttpResult(cache_key, 304, not_modified=True, data=None)
        data = self._json(response, policy, url)
        if conditional and self.use_validators:
            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")
            if etag or last_modified:
                self.store.set_http_validators(
                    cache_key, HttpValidators(etag, last_modified), self.clock()
                )
        return HttpResult(cache_key, response.status_code, False, data, dict(response.headers))

    async def post_form(
        self,
        url: str,
        policy: RequestPolicy,
        data: Mapping[str, str],
        *,
        params: Mapping[str, Any] | None = None,
    ) -> HttpResult:
        """POST a form (e.g. an OAuth token request) and parse the JSON reply."""
        response = await self._request("POST", url, policy, params=params, data=data)
        return HttpResult(
            url,
            response.status_code,
            False,
            self._json(response, policy, url),
            dict(response.headers),
        )


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status_code: int
    location: str | None


_PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


async def _probe_once(http: HttpClient, url: str, policy: RequestPolicy) -> ProbeResult:
    host = httpx.URL(url).host
    async with http._semaphore(host), http._total:
        http.reserve(policy)
        await http._limiter.acquire(host, policy.per_minute(host))
        response = await http.client.request(
            "HEAD", url, headers=_PAGE_HEADERS, follow_redirects=False
        )
    if response.status_code in (405, 501):  # HEAD refused: GET, but never read the body
        async with http._semaphore(host), http._total:
            http.reserve(policy)
            await http._limiter.acquire(host, policy.per_minute(host))
            async with http.client.stream(
                "GET", url, headers=_PAGE_HEADERS, follow_redirects=False
            ) as streamed:
                response = streamed
    if response.status_code == 429 or response.status_code >= 500:
        raise _RetryableStatusError(response)
    return ProbeResult(url, response.status_code, response.headers.get("location"))


async def probe(http: HttpClient, url: str, policy: RequestPolicy) -> ProbeResult:
    """One hop: HEAD (or a streamed GET if HEAD is refused) without following redirects
    and without reading a body. robots.txt is checked first; every request counts against
    the policy's caps. Timeouts, 5xx and 429 are retried; any final HTTP status is
    returned rather than raised."""
    if not await http._robots.allowed(url):
        raise RobotsDisallowedError(f"{policy.source}: robots.txt disallows {url}")
    result: ProbeResult | None = None
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            retry=retry_if_exception(http._should_retry),
            wait=http._wait,
            sleep=http.sleep,
            reraise=True,
        ):
            with attempt:
                result = await _probe_once(http, url, policy)
    except _RetryableStatusError as exc:
        http._count_error(policy)
        return ProbeResult(url, exc.response.status_code, None)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        http._count_error(policy)
        raise FetchError(f"{policy.source}: {type(exc).__name__} for {url}") from exc
    if result is None:  # pragma: no cover - AsyncRetrying always runs once
        raise FetchError(f"{policy.source}: no response from {url}")
    return result
