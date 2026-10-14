"""Keeping listing_status current, and resolving apply links.

- Full-feed sources (ATS boards, Remote OK, Remotive) return every open job each time,
  so a job missing from two consecutive *complete* fetches is marked expired. A fetch
  with any error or any 304 Not Modified does not count. Removing a company from
  ats_companies therefore expires its jobs after two runs.
- Jobs from other sources are re-checked at most weekly, and only when their
  relevance_score is at least 50: 404/410 (or a redirect away from the posting to a
  listing page, a "soft 404") -> expired, 2xx -> still active, three other failures in
  a row -> inaccessible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from jobfinder.config import SourceName
from jobfinder.http.client import CapReachedError, FetchError, HttpClient, RequestPolicy
from jobfinder.http.links import LinkResolver, follow, liveness_check
from jobfinder.models import Decision, ListingStatus, Opportunity
from jobfinder.pipeline.fetch import FetchStatus, SourceFetch
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.state_store import LivenessRecord, StateStore

FULL_FEED_SOURCES = frozenset(
    {
        SourceName.GREENHOUSE.value,
        SourceName.LEVER.value,
        SourceName.ASHBY.value,
        SourceName.REMOTEOK.value,
        SourceName.REMOTIVE.value,
    }
)
ABSENT_RUNS_TO_EXPIRE = 2
RECHECK_INTERVAL = timedelta(days=7)
LIVENESS_MIN_SCORE = 50
FAILURES_TO_INACCESSIBLE = 3
GONE_STATUSES = frozenset({404, 410})


# --------------------------------------------------------------------------- full feeds


def is_complete_feed(fetch: SourceFetch) -> bool:
    return (
        fetch.name.value in FULL_FEED_SOURCES
        and fetch.status is FetchStatus.OK
        and fetch.report.complete
        and not fetch.report.not_modified
    )


def expire_missing(
    repo: OpportunityRepository, store: StateStore, fetch: SourceFetch, now: datetime
) -> list[Opportunity]:
    """Count absences for one full-feed fetch; return the opportunities now expired."""
    if not is_complete_feed(fetch):
        return []
    seen = {job.source_job_id for job in fetch.jobs}
    expired = []
    for opportunity in list(repo.iter_active()):
        if opportunity.source != fetch.name.value:
            continue
        if opportunity.source_job_id in seen:
            store.set_absence(opportunity.opportunity_id, 0, now)
            continue
        misses = store.get_absence(opportunity.opportunity_id) + 1
        if misses >= ABSENT_RUNS_TO_EXPIRE:
            repo.mark_status(opportunity.opportunity_id, ListingStatus.EXPIRED)
            store.set_absence(opportunity.opportunity_id, 0, now)
            expired.append(opportunity)
        else:
            store.set_absence(opportunity.opportunity_id, misses, now)
    return expired


# --------------------------------------------------------------------------- liveness


@dataclass
class LivenessStats:
    checked: int = 0
    alive: int = 0
    expired: list[Opportunity] = field(default_factory=list)
    inaccessible: list[Opportunity] = field(default_factory=list)
    unverifiable: int = 0
    failures: int = 0
    stopped: str | None = None


_LISTING_ROOTS = frozenset(
    {"", "jobs", "careers", "career", "join", "join-us", "openings", "positions"}
)


def redirected_away(original: str, walk_final: str | None, hops: int) -> bool:
    """A 'soft 404': the posting redirected to a listing page instead of itself.

    Greenhouse, for example, answers a closed job with a redirect to
    ``/<board>?error=true``; the final page is a 200 but the job is gone.
    """
    if hops == 0 or walk_final is None:
        return False
    before, after = urlsplit(original), urlsplit(walk_final)
    if any(key.lower() in ("error", "error_code") for key, _ in parse_qsl(after.query)):
        return True
    old = [p for p in before.path.split("/") if p]
    new = [p for p in after.path.split("/") if p]
    if before.hostname == after.hostname and len(new) < len(old) and old[: len(new)] == new:
        return True  # moved up to an ancestor page, e.g. /acme/jobs/123 -> /acme
    return len(new) <= 1 and (not new or new[0].lower() in _LISTING_ROOTS)


def check_url(opportunity: Opportunity) -> str | None:
    """The best URL to check: the employer page if known, else the posting itself."""
    for url in (opportunity.apply_url_final, opportunity.apply_url, opportunity.posting_url):
        if url and liveness_check(url) is None:
            return url
    return None


def liveness_due(opportunity: Opportunity, store: StateStore, now: datetime) -> bool:
    if (
        opportunity.listing_status is not ListingStatus.ACTIVE
        or opportunity.source in FULL_FEED_SOURCES
        or (opportunity.relevance_score or 0) < LIVENESS_MIN_SCORE
    ):
        return False
    record = store.get_liveness(opportunity.opportunity_id)
    since = record.last_checked_at if record else opportunity.discovered_at
    return now - since >= RECHECK_INTERVAL


async def check_liveness(
    repo: OpportunityRepository,
    store: StateStore,
    http: HttpClient,
    policy: RequestPolicy,
    now: datetime,
    limit: int,
) -> LivenessStats:
    stats = LivenessStats()
    due = [o for o in repo.iter_active() if liveness_due(o, store, now)]
    due.sort(
        key=lambda o: (
            (
                store.get_liveness(o.opportunity_id) or LivenessRecord(o.discovered_at, 0, None)
            ).last_checked_at
        )
    )
    for opportunity in due[:limit]:
        previous = store.get_liveness(opportunity.opportunity_id)
        failures = previous.failures if previous else 0
        url = check_url(opportunity)
        if url is None:
            stats.unverifiable += 1
            store.set_liveness(opportunity.opportunity_id, LivenessRecord(now, failures, None))
            continue
        try:
            walk = await follow(http, url, policy, liveness_check)
        except CapReachedError as exc:
            stats.stopped = str(exc)
            break
        except FetchError:
            walk = None
        stats.checked += 1
        status = walk.status if walk else None
        if walk is not None and walk.status is None:
            # Ended at robots.txt, a forbidden site or too many redirects: no verdict.
            stats.unverifiable += 1
            store.set_liveness(opportunity.opportunity_id, LivenessRecord(now, failures, None))
        elif status in GONE_STATUSES or (
            walk is not None
            and status is not None
            and 200 <= status < 300
            and redirected_away(url, walk.final_url, walk.hops)
        ):
            repo.mark_status(opportunity.opportunity_id, ListingStatus.EXPIRED)
            store.set_liveness(opportunity.opportunity_id, LivenessRecord(now, 0, status))
            stats.expired.append(opportunity)
        elif status is not None and 200 <= status < 300:
            store.set_liveness(opportunity.opportunity_id, LivenessRecord(now, 0, status))
            stats.alive += 1
        else:
            failures += 1
            stats.failures += 1
            store.set_liveness(opportunity.opportunity_id, LivenessRecord(now, failures, status))
            if failures >= FAILURES_TO_INACCESSIBLE:
                repo.mark_status(opportunity.opportunity_id, ListingStatus.INACCESSIBLE)
                stats.inaccessible.append(opportunity)
    return stats


# --------------------------------------------------------------------------- apply links


@dataclass
class LinkStats:
    resolved: int = 0
    unresolvable: int = 0
    deferred: int = 0
    stopped: str | None = None


_PRIORITY = {Decision.STRONG: 0, Decision.POSSIBLE: 1}  # only jobs worth applying to


async def resolve_apply_links(
    repo: OpportunityRepository, resolver: LinkResolver, limit: int
) -> LinkStats:
    """Fill apply_url_final for active strong and possible jobs, strongest first."""
    stats = LinkStats()
    todo = [
        (o, o.apply_url)
        for o in repo.iter_active()
        if o.apply_url and o.apply_url_final is None and o.decision in _PRIORITY
    ]
    todo.sort(key=lambda pair: (_PRIORITY[pair[0].decision], -(pair[0].relevance_score or 0)))
    for opportunity, url in todo[:limit]:
        try:
            record = await resolver.resolve(url)
        except CapReachedError as exc:
            stats.stopped = str(exc)
            break
        if record is None:
            stats.deferred += 1
        elif record.final_url is None:
            stats.unresolvable += 1
        else:
            repo.upsert(opportunity.model_copy(update={"apply_url_final": record.final_url}))
            stats.resolved += 1
    return stats
