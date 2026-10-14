"""robots.txt checks for requests that are not documented API endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from jobfinder.storage.state_store import StateStore

CACHE_TTL = timedelta(hours=24)
ROBOTS_AGENT = "jobfinder"

# fetch(url) -> (status, body); raises on network failure
RobotsFetcher = Callable[[str], Awaitable[tuple[int, str]]]


def parser_for(status: int, body: str) -> RobotFileParser:
    """Standard interpretation: 2xx parse; 401/403 and 5xx disallow all; other 4xx
    (no robots.txt) allow all."""
    parser = RobotFileParser()
    if 200 <= status < 300:
        parser.parse(body.splitlines())
    elif status in (401, 403) or status >= 500:
        parser.parse(["User-agent: *", "Disallow: /"])
    else:
        parser.parse([])  # no rules: everything allowed
    return parser


@dataclass
class RobotsCache:
    """robots.txt per host, cached for 24 hours in the state store.

    A server error or network failure while fetching robots.txt is treated as
    'disallow' for this request and is not cached, so it is retried next time.
    """

    store: StateStore
    fetch: RobotsFetcher
    clock: Callable[[], datetime]
    _parsed: dict[str, RobotFileParser] = field(default_factory=dict)

    async def _parser(self, scheme: str, host: str) -> RobotFileParser:
        now = self.clock()
        cached = self.store.get_robots(host)
        if cached is not None and now - cached[2] < CACHE_TTL:
            if host not in self._parsed:
                self._parsed[host] = parser_for(cached[0], cached[1])
            return self._parsed[host]
        try:
            status, body = await self.fetch(f"{scheme}://{host}/robots.txt")
        except Exception:  # network trouble: disallow, retry later
            return parser_for(503, "")
        if status >= 500:
            return parser_for(status, "")
        self.store.put_robots(host, status, body, now)
        self._parsed[host] = parser_for(status, body)
        return self._parsed[host]

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return False
        parser = await self._parser(parts.scheme, parts.netloc.lower())
        return parser.can_fetch(ROBOTS_AGENT, url)
