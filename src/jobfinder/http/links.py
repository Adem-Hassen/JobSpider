"""Apply-link resolution and liveness probes.

Links come from untrusted job data, so every hop is checked before any request:
- only http(s), no credentials in the URL, no IP literals, no local or private names;
- never LinkedIn, Indeed or Glassdoor (they forbid automated access);
- resolution only on employer and ATS hosts: aggregator links (Adzuna's redirect_url,
  Remotive, Remote OK, Jobicy, Himalayas, Arbeitnow, France Travail) are never resolved;
- robots.txt is checked for every hop, by the HTTP layer.
At most MAX_HOPS redirects are followed, using HEAD (or a GET whose body is never read).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from jobfinder.config import LinkSettings
from jobfinder.http.client import (
    CapReachedError,
    FetchError,
    HttpClient,
    RequestPolicy,
    RobotsDisallowedError,
    probe,
)
from jobfinder.http.identity import never_fetch
from jobfinder.storage.state_store import LinkRecord, StateStore

MAX_HOPS = 5
LINKS_SOURCE = "links"

# Registrable names of job aggregators whose links must not be resolved.
AGGREGATOR_NAMES = frozenset(
    {"adzuna", "remotive", "remoteok", "jobicy", "himalayas", "arbeitnow", "francetravail"}
)
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp")


def links_policy(settings: LinkSettings) -> RequestPolicy:
    return RequestPolicy(
        source=LINKS_SOURCE,
        daily_request_cap=settings.daily_request_cap,
        requests_per_minute={},
        max_per_minute=settings.requests_per_minute,
    )


def unsafe_reason(url: str) -> str | None:
    """Why ``url`` must not be requested at all, or None if it may be."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed URL"
    if parts.scheme not in ("http", "https"):
        return f"scheme {parts.scheme or '(none)'} not allowed"
    if parts.username or parts.password:
        return "credentials in URL"
    host = (parts.hostname or "").rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "IP address hosts are not followed"
    if not host or "." not in host:
        return "not a public host name"
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return "local host name"
    if port not in (None, 80, 443):
        return f"non-standard port {port}"
    if never_fetch(host):
        return "site forbids automated access"
    return None


def is_aggregator(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(part in AGGREGATOR_NAMES for part in host.split(".")[:-1])


@dataclass(frozen=True)
class Walk:
    """Where following a link ended. ``status`` is None when no final page was fetched."""

    final_url: str | None
    status: int | None
    hops: int
    note: str | None = None


async def follow(
    http: HttpClient,
    url: str,
    policy: RequestPolicy,
    allowed: Callable[[str], str | None],
) -> Walk:
    """Follow redirects from ``url`` while ``allowed`` (a reason-or-None check) permits."""
    current = url
    for hop in range(MAX_HOPS + 1):
        reason = allowed(current)
        if reason is not None:
            # Stop before requesting a host we may not touch; its URL is still the
            # best-known destination when we were redirected there.
            return Walk(current if hop else None, None, hop, f"stopped: {reason}")
        try:
            result = await probe(http, current, policy)
        except RobotsDisallowedError:
            return Walk(current if hop else None, None, hop, "robots.txt disallows")
        if 300 <= result.status_code < 400 and result.location:
            current = urljoin(current, result.location)
            continue
        return Walk(current, result.status_code, hop)
    return Walk(None, None, MAX_HOPS, f"more than {MAX_HOPS} redirects")


def resolution_check(url: str) -> str | None:
    return unsafe_reason(url) or ("aggregator link" if is_aggregator(url) else None)


@dataclass
class LinkResolver:
    """Resolves apply links to their final employer/ATS page; results cached forever."""

    http: HttpClient
    store: StateStore
    policy: RequestPolicy
    clock: Callable[[], datetime]

    async def resolve(self, url: str) -> LinkRecord | None:
        """The cached or newly resolved destination; None if it could not be resolved
        this time (network trouble or caps; retried on a later run)."""
        cached = self.store.get_link(url)
        if cached is not None:
            return cached
        if (reason := resolution_check(url)) is not None:
            record = LinkRecord(None, None, f"not resolved: {reason}", self.clock())
            self.store.put_link(url, record)
            return record
        try:
            walk = await follow(self.http, url, self.policy, resolution_check)
        except CapReachedError:
            raise
        except FetchError:
            return None
        if walk.status is not None and not 200 <= walk.status < 300:
            return None  # error page: try again on a later run
        record = LinkRecord(walk.final_url, walk.status, walk.note, self.clock())
        self.store.put_link(url, record)
        return record


def liveness_check(url: str) -> str | None:
    """Liveness may look at aggregator pages too, but never resolves Adzuna redirects."""
    reason = unsafe_reason(url)
    if reason is None and "adzuna" in (urlsplit(url).hostname or "").split("."):
        return "Adzuna redirect links are not resolved"
    return reason
