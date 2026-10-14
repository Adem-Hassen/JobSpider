"""Arbeitnow job board API: GET https://www.arbeitnow.com/api/job-board-api?page=N.

Free public API ("please do not abuse"; linking back is appreciated). 300 jobs per
page, newest first; paging stops at the first page whose jobs are all known.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobfinder.config import ArbeitnowQuery, PagingOptions
from jobfinder.models import RawJob
from jobfinder.pipeline.normalise import employment_type_of
from jobfinder.sources.base import FetchContext, parse_epoch_s, parse_records, text
from jobfinder.sources.paged import fetch_pages
from jobfinder.storage.state_store import SourceState

API = "https://www.arbeitnow.com/api/job-board-api"


def _employment_hint(job_types: Any, title: str) -> str | None:
    for value in job_types or []:
        if isinstance(value, str) and (kind := employment_type_of(value, "")) is not None:
            return kind.value
    return None


def parse_job(record: Mapping[str, Any]) -> RawJob | None:
    slug = text(record.get("slug"))
    url = text(record.get("url"))
    title = text(record.get("title"))
    company = text(record.get("company_name"))
    if slug is None or url is None or title is None or company is None:
        return None
    location = text(record.get("location"))
    return RawJob(
        source="arbeitnow",
        source_job_id=slug,
        is_ats=False,
        title=title,
        company=company,
        locations=[location] if location else [],
        remote_hint="remote" if record.get("remote") is True else None,
        employment_type_hint=_employment_hint(record.get("job_types"), title),
        description_html=text(record.get("description")),
        posting_url=url,
        published_at=parse_epoch_s(record.get("created_at")),
        tags=[t for t in record.get("tags") or [] if isinstance(t, str)],
    )


class ArbeitnowSource:
    name = "arbeitnow"

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        options = PagingOptions.model_validate(ctx.options)
        jobs: list[RawJob] = []
        for raw_query in ctx.queries or [{}]:
            query = ArbeitnowQuery.model_validate(raw_query)
            base = {k: str(v).lower() for k, v in query.model_dump(exclude_none=True).items()}

            async def page(
                token: str | None, base: dict[str, str] = base
            ) -> tuple[list[RawJob], str | None]:
                number = int(token or "1")
                result = await ctx.http.get_json(
                    API, ctx.policy, params={**base, "page": number}, conditional=number == 1
                )
                if result.not_modified:
                    ctx.report.not_modified.append(result.url)
                    return [], None
                data = result.data
                has_next = bool((data.get("links") or {}).get("next"))
                return parse_records(data["data"], parse_job, ctx.report), str(
                    number + 1
                ) if has_next else None

            jobs.extend(await fetch_pages(ctx, page, options.max_pages))
        return jobs
