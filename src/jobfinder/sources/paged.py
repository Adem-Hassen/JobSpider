"""Shared loop for sources that page through a feed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from jobfinder.http.client import CapReachedError, FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext

# fetch_page(page_token) -> (jobs, next_token); next_token None means last page
PageFetcher = Callable[[str | None], Awaitable[tuple[list[RawJob], str | None]]]


async def fetch_pages(
    ctx: FetchContext, fetch_page: PageFetcher, max_pages: int, *, label: str = ""
) -> list[RawJob]:
    """Page until the last page, ``max_pages``, or a page whose jobs are all known.

    A failure on a later page keeps the jobs already fetched (reported as an error).
    """
    jobs: list[RawJob] = []
    token: str | None = None
    prefix = f"{label}: " if label else ""
    for _ in range(max_pages):
        try:
            page, token = await fetch_page(token)
        except (SourceAuthError, CapReachedError):
            if jobs:  # keep what we have; the cap or auth problem is reported
                ctx.report.errors.append(f"{prefix}stopped paging early")
                return jobs
            raise
        except FetchError as exc:
            ctx.report.errors.append(f"{prefix}{exc}")
            return jobs
        except (KeyError, TypeError, ValueError) as exc:
            ctx.report.errors.append(f"{prefix}unexpected response shape ({exc})")
            return jobs
        jobs.extend(page)
        if token is None or not page or ctx.all_known(page):
            break
    return jobs
