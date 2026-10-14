"""Himalayas jobs API: GET https://himalayas.app/jobs/api?limit=20&cursor=... .

Personal use. Terms (https://himalayas.app/api): link back to the Himalayas URL and
mention Himalayas as the source. Data is refreshed once a day, so polling more often
gains nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from jobfinder.config import PagingOptions
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, parse_epoch_s, parse_records, text
from jobfinder.sources.paged import fetch_pages
from jobfinder.storage.state_store import SourceState

API = "https://himalayas.app/jobs/api"
PAGE_SIZE = 20  # the API maximum


def parse_job(record: Mapping[str, Any]) -> RawJob | None:
    guid = text(record.get("guid"))
    url = text(record.get("applicationLink")) or guid
    title = text(record.get("title"))
    company = text(record.get("companyName"))
    if guid is None or url is None or title is None or company is None:
        return None
    restrictions = [r for r in record.get("locationRestrictions") or [] if isinstance(r, str)]
    categories = record.get("parentCategories") or record.get("categories") or []
    return RawJob(
        source="himalayas",
        # guid is the job's Himalayas URL; its path is a stable, shorter id
        source_job_id=urlsplit(guid).path.strip("/") or guid,
        is_ats=False,
        title=title,
        company=company,
        locations=["Remote, " + "; Remote, ".join(restrictions)] if restrictions else ["Worldwide"],
        country_hints=restrictions,
        remote_hint="remote",
        employment_type_hint=text(record.get("employmentType")),
        description_html=text(record.get("description")),
        posting_url=url,  # the Himalayas URL, as their terms require
        published_at=parse_epoch_s(record.get("pubDate")),
        tags=[c for c in categories if isinstance(c, str)],
    )


class HimalayasSource:
    name = "himalayas"

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        options = PagingOptions.model_validate(ctx.options)

        async def page(cursor: str | None) -> tuple[list[RawJob], str | None]:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            result = await ctx.http.get_json(
                API, ctx.policy, params=params, conditional=cursor is None
            )
            if result.not_modified:
                ctx.report.not_modified.append(result.url)
                return [], None
            data = result.data
            return parse_records(data["jobs"], parse_job, ctx.report), text(data.get("nextCursor"))

        return await fetch_pages(ctx, page, options.max_pages)
