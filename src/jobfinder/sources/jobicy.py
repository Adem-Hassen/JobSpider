"""Jobicy remote jobs API v2: GET https://jobicy.com/api/v2/remote-jobs (cursor-paged).

Terms (https://jobi.cy/apidocs): at most one sync pass per hour; credit Jobicy and keep
the Jobicy job URL. The feed covers the last 7 days.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobfinder.config import JobicyQuery, PagingOptions
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, parse_iso, parse_records, text
from jobfinder.sources.paged import fetch_pages
from jobfinder.storage.state_store import SourceState

API = "https://jobicy.com/api/v2/remote-jobs"
PAGE_SIZE = 50


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        return next((text(v) for v in value if text(v)), None)
    return text(value)


def parse_job(record: Mapping[str, Any]) -> RawJob | None:
    url = text(record.get("url"))
    title = text(record.get("jobTitle"))
    company = text(record.get("companyName"))
    if url is None or title is None or company is None or record.get("id") is None:
        return None
    geo = text(record.get("jobGeo"))
    industries = record.get("jobIndustry") or []
    return RawJob(
        source="jobicy",
        source_job_id=str(record["id"]),
        is_ats=False,
        title=title,
        company=company,
        locations=[geo] if geo else [],
        remote_hint="remote",
        employment_type_hint=_first(record.get("jobType")),
        description_html=text(record.get("jobDescription")),
        posting_url=url,  # the Jobicy URL, as their terms require
        published_at=parse_iso(record.get("pubDate")),
        tags=[t for t in industries if isinstance(t, str)],
    )


class JobicySource:
    name = "jobicy"

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        options = PagingOptions.model_validate(ctx.options)
        jobs: list[RawJob] = []
        for raw_query in ctx.queries or [{}]:
            query = JobicyQuery.model_validate(raw_query)
            params = {"count": PAGE_SIZE, **query.model_dump(exclude_none=True)}

            async def page(
                cursor: str | None, params: dict[str, Any] = params
            ) -> tuple[list[RawJob], str | None]:
                result = await ctx.http.get_json(
                    API,
                    ctx.policy,
                    params={**params, **({"cursor": cursor} if cursor else {})},
                    conditional=cursor is None,
                )
                if result.not_modified:
                    ctx.report.not_modified.append(result.url)
                    return [], None
                data = result.data
                parsed = parse_records(data["jobs"], parse_job, ctx.report)
                return parsed, text(data.get("nextCursor")) if data.get("hasMore", True) else None

            jobs.extend(await fetch_pages(ctx, page, options.max_pages, label=str(params)))
        return jobs
