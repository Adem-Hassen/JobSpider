"""Adzuna search API: GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}.

Terms (https://developer.adzuna.com/docs/terms_of_service): 25 requests/minute,
250/day, 1000/week, 2500/month (the daily cap in sources.yaml is lower). redirect_url
is stored as the posting URL and is never resolved. Descriptions are snippets.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from jobfinder.config import AdzunaQuery, PagingOptions
from jobfinder.http.client import CapReachedError, FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, parse_iso, parse_records, text
from jobfinder.storage.state_store import SourceState

API = "https://api.adzuna.com/v1/api/jobs"


def parse_job(record: Mapping[str, Any], country: str) -> RawJob | None:
    url = text(record.get("redirect_url"))
    title = text(record.get("title"))
    if url is None or title is None or record.get("id") is None:
        return None
    company = text((record.get("company") or {}).get("display_name")) or "Unknown company"
    location = record.get("location") or {}
    area = [a for a in location.get("area") or [] if isinstance(a, str)]
    display = text(location.get("display_name"))
    contract_time = text(record.get("contract_time"))  # full_time | part_time
    contract_type = text(record.get("contract_type"))  # permanent | contract
    return RawJob(
        source="adzuna",
        source_job_id=str(record["id"]),
        is_ats=False,
        title=title,  # may contain <strong> highlights; normalisation unescapes
        company=company,
        locations=[", ".join(reversed(area[1:]))]
        if len(area) > 1
        else ([display] if display else []),
        country_hints=[country.upper()],
        employment_type_hint=contract_type if contract_type == "contract" else contract_time,
        description_html=text(record.get("description")),
        posting_url=url,  # never resolved
        published_at=parse_iso(record.get("created")),
        tags=[t for t in [text((record.get("category") or {}).get("label"))] if t],
    )


class AdzunaSource:
    name = "adzuna"

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        options = PagingOptions.model_validate(ctx.options)
        secret = {
            "app_id": ctx.credentials.get("ADZUNA_APP_ID", ""),
            "app_key": ctx.credentials.get("ADZUNA_APP_KEY", ""),
        }
        jobs: list[RawJob] = []
        for raw_query in ctx.queries:
            query = AdzunaQuery.model_validate(raw_query)
            params = query.model_dump(exclude_none=True, exclude={"country"})
            params["content-type"] = "application/json"
            for page in range(1, options.max_pages + 1):
                url = f"{API}/{query.country}/search/{page}"
                try:
                    result = await ctx.http.get_json(
                        url, ctx.policy, params=params, secret_params=secret, conditional=False
                    )
                except (SourceAuthError, CapReachedError):
                    if jobs:
                        ctx.report.errors.append(f"{query.country}: stopped early")
                        return jobs
                    raise
                except FetchError as exc:
                    ctx.report.errors.append(str(exc))
                    break
                try:
                    results = result.data["results"]
                    page_jobs = parse_records(
                        results, partial(parse_job, country=query.country), ctx.report
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    ctx.report.errors.append(f"{query.country}: unexpected response shape ({exc})")
                    break
                jobs.extend(page_jobs)
                if len(results) < query.results_per_page or ctx.all_known(page_jobs):
                    break
        return jobs
