"""Remotive public API: GET https://remotive.com/api/remote-jobs.

Terms (https://remotive.com/api-documentation): at most 4 requests a day and never more
than 2 a minute (enforced by sources.yaml ceilings); always link back to the Remotive
job URL and name Remotive as the source; listings are delayed by 24 hours.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobfinder.http.client import CapReachedError, FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, FetchReport, parse_iso, parse_records, text
from jobfinder.storage.state_store import SourceState

API = "https://remotive.com/api/remote-jobs"
ALLOWED_PARAMS = frozenset({"category", "search", "company_name", "limit"})


def parse_job(record: Mapping[str, Any]) -> RawJob | None:
    url = text(record.get("url"))
    title = text(record.get("title"))
    company = text(record.get("company_name"))
    if url is None or title is None or company is None or record.get("id") is None:
        return None
    location = text(record.get("candidate_required_location"))
    return RawJob(
        source="remotive",
        source_job_id=str(record["id"]),
        is_ats=False,
        title=title,
        company=company,
        locations=[location] if location else [],
        remote_hint="remote",
        employment_type_hint=text(record.get("job_type")),
        description_html=text(record.get("description")),
        posting_url=url,  # always the Remotive URL, as their terms require
        apply_url=None,
        # Remotive gives no timezone; treated as UTC.
        published_at=parse_iso(record.get("publication_date")),
        tags=[t for t in record.get("tags") or [] if isinstance(t, str)],
    )


def parse_response(payload: Any, report: FetchReport) -> list[RawJob]:
    return parse_records(payload["jobs"], parse_job, report)


class RemotiveSource:
    name = "remotive"

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        jobs: list[RawJob] = []
        for query in ctx.queries or [{}]:
            params = {k: v for k, v in query.items() if k in ALLOWED_PARAMS}
            try:
                result = await ctx.http.get_json(API, ctx.policy, params=params or None)
            except (SourceAuthError, CapReachedError):
                raise
            except FetchError as exc:
                ctx.report.errors.append(str(exc))
                continue
            if result.not_modified:
                ctx.report.not_modified.append(result.url)
                continue
            try:
                jobs.extend(parse_response(result.data, ctx.report))
            except (KeyError, TypeError, ValueError) as exc:
                ctx.report.errors.append(f"unexpected response shape ({exc})")
        return jobs
