from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from jobfinder.config import LinkSettings, SourceName
from jobfinder.http.client import HttpClient, RequestPolicy, create_async_client
from jobfinder.http.links import LinkResolver, links_policy
from jobfinder.models import Decision, ListingStatus, Opportunity, UserFields
from jobfinder.pipeline.fetch import FetchStatus, SourceFetch
from jobfinder.pipeline.liveness import (
    check_liveness,
    expire_missing,
    redirected_away,
    resolve_apply_links,
)
from jobfinder.sources.base import FetchReport
from jobfinder.storage.sql_repo import SqlRepository
from jobfinder.storage.state_store import LivenessRecord, StateStore
from tests.helpers import FakeClock, make_raw_job
from tests.unit.test_repositories import make

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)
POLICY = links_policy(LinkSettings(requests_per_minute=60))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StateStore]:
    with StateStore(tmp_path / "state.db") as s:
        yield s


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[SqlRepository]:
    r = SqlRepository.sqlite(tmp_path / "o.db")
    yield r
    r.close()


@pytest.fixture
async def http(store: StateStore) -> AsyncIterator[HttpClient]:
    clock = FakeClock(T0)
    async with create_async_client("me@example.com") as client:
        yield HttpClient(client, store, clock=clock, sleep=clock.sleep)


def opp(n: int, **kw: Any) -> Opportunity:
    data: dict[str, Any] = {
        "source": "greenhouse",
        "source_job_id": f"g{n}",
        "canonical_url": f"https://job-boards.greenhouse.io/acme/jobs/{n}",
        "posting_url": f"https://job-boards.greenhouse.io/acme/jobs/{n}",
        "apply_url": None,
        "discovered_at": T0 - timedelta(days=30),
        "last_seen_at": T0 - timedelta(days=1),
    }
    data.update(kw)
    return make(f"01J00000000000000000000{n:03d}", **data)


def fetched(name: SourceName, ids: list[str], **report: Any) -> SourceFetch:
    jobs = [
        make_raw_job(source=name.value, source_job_id=i, posting_url=f"https://x.test/{i}")
        for i in ids
    ]
    return SourceFetch(name=name, jobs=jobs, report=FetchReport(**report))


# --------------------------------------------------------------------------- full feeds


def test_two_complete_absences_expire(repo: SqlRepository, store: StateStore) -> None:
    for n in (1, 2):
        repo.upsert(opp(n))
    assert expire_missing(repo, store, fetched(SourceName.GREENHOUSE, ["g1"]), T0) == []
    expired = expire_missing(repo, store, fetched(SourceName.GREENHOUSE, ["g1"]), T0)
    assert [o.source_job_id for o in expired] == ["g2"]
    assert {o.source_job_id for o in repo.iter_active()} == {"g1"}


def test_reappearing_resets_the_count(repo: SqlRepository, store: StateStore) -> None:
    repo.upsert(opp(1))
    expire_missing(repo, store, fetched(SourceName.GREENHOUSE, []), T0)
    expire_missing(repo, store, fetched(SourceName.GREENHOUSE, ["g1"]), T0)
    assert expire_missing(repo, store, fetched(SourceName.GREENHOUSE, []), T0) == []
    assert store.get_absence(opp(1).opportunity_id) == 1


@pytest.mark.parametrize(
    "result",
    [
        fetched(SourceName.GREENHOUSE, [], errors=["Nope: HTTP 404"]),
        fetched(SourceName.GREENHOUSE, [], not_modified=["https://boards-api..."]),
        SourceFetch(SourceName.GREENHOUSE, status=FetchStatus.FAILED, fatal="boom"),
        SourceFetch(SourceName.GREENHOUSE, status=FetchStatus.CAPPED, fatal="cap"),
        fetched(SourceName.ARBEITNOW, []),  # not a full-feed source
    ],
)
def test_incomplete_fetches_never_expire(
    repo: SqlRepository, store: StateStore, result: SourceFetch
) -> None:
    repo.upsert(opp(1))
    repo.upsert(opp(2, source="arbeitnow", source_job_id="a2"))
    for _ in range(3):
        assert expire_missing(repo, store, result, T0) == []
    assert repo.count() == len(list(repo.iter_active()))


def test_only_the_fetched_source_is_affected(repo: SqlRepository, store: StateStore) -> None:
    repo.upsert(opp(1, source="lever", source_job_id="L1"))
    for _ in range(3):
        expire_missing(repo, store, fetched(SourceName.GREENHOUSE, []), T0)
    assert len(list(repo.iter_active())) == 1


# --------------------------------------------------------------------------- liveness


def other(n: int, **kw: Any) -> Opportunity:
    data: dict[str, Any] = {
        "source": "arbeitnow",
        "source_job_id": f"a{n}",
        "canonical_url": f"https://www.arbeitnow.com/jobs/x-{n}",
        "posting_url": f"https://www.arbeitnow.com/jobs/x-{n}",
        "apply_url": None,
        "relevance_score": 70,
        "decision": Decision.POSSIBLE,
        "discovered_at": T0 - timedelta(days=8),
    }
    data.update(kw)
    return opp(n, **data)


def no_robots() -> None:
    respx.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)


@respx.mock
async def test_404_and_soft_404_expire(
    repo: SqlRepository, store: StateStore, http: HttpClient
) -> None:
    no_robots()
    repo.upsert(other(1))
    repo.upsert(other(2, apply_url_final="https://job-boards.greenhouse.io/acme/jobs/77"))
    respx.head("https://www.arbeitnow.com/jobs/x-1").respond(410)
    respx.head("https://job-boards.greenhouse.io/acme/jobs/77").respond(
        302, headers={"Location": "https://job-boards.greenhouse.io/acme?error=true"}
    )
    respx.head("https://job-boards.greenhouse.io/acme?error=true").respond(200)
    stats = await check_liveness(repo, store, http, POLICY, T0, limit=10)
    assert {o.source_job_id for o in stats.expired} == {"a1", "a2"}
    assert list(repo.iter_active()) == []


@respx.mock
async def test_alive_then_weekly(repo: SqlRepository, store: StateStore, http: HttpClient) -> None:
    no_robots()
    repo.upsert(other(1))
    route = respx.head("https://www.arbeitnow.com/jobs/x-1").respond(200)
    stats = await check_liveness(repo, store, http, POLICY, T0, limit=10)
    assert stats.alive == 1
    await check_liveness(repo, store, http, POLICY, T0 + timedelta(days=6), limit=10)
    assert route.call_count == 1  # not again within a week
    await check_liveness(repo, store, http, POLICY, T0 + timedelta(days=7), limit=10)
    assert route.call_count == 2


@respx.mock
async def test_three_failures_inaccessible(
    repo: SqlRepository, store: StateStore, http: HttpClient
) -> None:
    no_robots()
    repo.upsert(other(1))
    respx.head("https://www.arbeitnow.com/jobs/x-1").respond(403)
    for week in range(3):
        stats = await check_liveness(
            repo, store, http, POLICY, T0 + timedelta(days=7 * week), limit=10
        )
    assert [o.source_job_id for o in stats.inaccessible] == ["a1"]
    found = repo.get_by_source_key("arbeitnow", "a1")
    assert found is not None and found.listing_status is ListingStatus.INACCESSIBLE


@respx.mock
async def test_who_gets_checked(repo: SqlRepository, store: StateStore, http: HttpClient) -> None:
    no_robots()
    repo.upsert(other(1, relevance_score=49))  # below 50
    repo.upsert(other(2, discovered_at=T0 - timedelta(days=2)))  # less than a week old
    repo.upsert(opp(3, relevance_score=90))  # full-feed source: handled by feed absence
    repo.upsert(
        other(
            4,
            posting_url="https://www.linkedin.com/jobs/view/4",
            canonical_url="https://www.linkedin.com/jobs/view/4",
            source="email_alerts",
        )
    )
    repo.upsert(
        other(
            5,
            posting_url="https://www.adzuna.fr/land/ad/5",
            canonical_url="https://www.adzuna.fr/land/ad/5",
            source="adzuna",
        )
    )
    stats = await check_liveness(repo, store, http, POLICY, T0, limit=10)
    assert stats.checked == 0
    assert stats.unverifiable == 2  # LinkedIn and Adzuna links are never requested
    assert len(respx.calls) == 0


@respx.mock
async def test_limit_and_cap(repo: SqlRepository, store: StateStore, http: HttpClient) -> None:
    no_robots()
    for n in range(1, 6):
        repo.upsert(other(n))
    respx.head(url__startswith="https://www.arbeitnow.com/jobs/").respond(200)
    stats = await check_liveness(repo, store, http, POLICY, T0, limit=2)
    assert stats.checked == 2
    tiny = RequestPolicy("links", 1, {})
    stats = await check_liveness(repo, store, http, tiny, T0, limit=10)
    assert stats.stopped is not None and "daily request cap" in stats.stopped


def test_redirected_away() -> None:
    assert redirected_away("https://h.io/a/jobs/1", "https://h.io/a?error=true", 1)
    assert redirected_away("https://h.io/a/jobs/1", "https://h.io/a", 1)
    assert redirected_away("https://acme.com/job/1", "https://acme.com/careers", 1)
    assert not redirected_away("https://h.io/a/jobs/1", "https://h2.io/a/jobs/1", 1)
    assert not redirected_away("https://h.io/a/jobs/1", "https://h.io/a/jobs/1", 0)


# --------------------------------------------------------------------------- apply links


@respx.mock
async def test_resolve_apply_links_strongest_first(
    repo: SqlRepository, store: StateStore, http: HttpClient
) -> None:
    no_robots()
    repo.upsert(
        opp(1, decision=Decision.POSSIBLE, relevance_score=60, apply_url="https://acme.com/a/1")
    )
    repo.upsert(
        opp(
            2,
            decision=Decision.STRONG,
            relevance_score=90,
            apply_url="https://acme.com/a/2",
            application_status="applied",
            notes="mine",
        )
    )
    repo.upsert(opp(3, decision=Decision.FILTERED, apply_url="https://acme.com/a/3"))
    repo.upsert(opp(4, decision=Decision.REJECT, apply_url="https://acme.com/a/4"))
    respx.head("https://acme.com/a/2").respond(
        301, headers={"Location": "https://acme.com/apply/2"}
    )
    respx.head("https://acme.com/apply/2").respond(200)
    respx.head("https://acme.com/a/1").respond(200)
    resolver = LinkResolver(http, store, POLICY, lambda: T0)
    stats = await resolve_apply_links(repo, resolver, limit=1)
    assert stats.resolved == 1
    strong = repo.get_by_source_key("greenhouse", "g2")
    assert strong is not None and strong.apply_url_final == "https://acme.com/apply/2"
    assert repo.user_fields(strong.opportunity_id) == UserFields(
        application_status="applied", notes="mine"
    )
    await resolve_apply_links(repo, resolver, limit=10)
    assert all(c.request.url.path not in ("/a/3", "/a/4") for c in respx.calls)
    possible = repo.get_by_source_key("greenhouse", "g1")
    assert possible is not None and possible.apply_url_final == "https://acme.com/a/1"


@respx.mock
async def test_resolution_stops_at_cap(
    repo: SqlRepository, store: StateStore, http: HttpClient
) -> None:
    repo.upsert(opp(1, decision=Decision.STRONG, apply_url="https://acme.com/a/1"))
    repo.upsert(opp(2, decision=Decision.STRONG, apply_url="https://acme.com/a/2"))
    no_robots()
    resolver = LinkResolver(http, store, RequestPolicy("links", 0, {}), lambda: T0)
    stats = await resolve_apply_links(repo, resolver, limit=10)
    assert stats.stopped is not None
    assert stats.resolved == 0


def test_liveness_record_roundtrip(store: StateStore) -> None:
    store.set_liveness("o1", LivenessRecord(T0, 2, 503))
    assert store.get_liveness("o1") == LivenessRecord(T0, 2, 503)
