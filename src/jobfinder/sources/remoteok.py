"""Remote OK: GET https://remoteok.com/api (the whole feed in one response).

Terms (the feed's first element): link back to the Remote OK URL with a followed link
and mention Remote OK as the source. The legal-notice element is skipped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobfinder.http.client import CapReachedError, FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, FetchReport, parse_iso, parse_records, text
from jobfinder.storage.state_store import SourceState

API = "https://remoteok.com/api"


def parse_job(record: Mapping[str, Any]) -> RawJob | None:
    if "legal" in record:
        return None
    url = text(record.get("url"))
    title = text(record.get("position"))
    company = text(record.get("company"))
    if url is None or title is None or company is None or record.get("id") is None:
        return None
    location = text(record.get("location"))
    return RawJob(
        source="remoteok",
        source_job_id=str(record["id"]),
        is_ats=False,
        title=title,
        company=company,
        locations=[location] if location else [],
        remote_hint="remote",
        description_html=text(record.get("description")),
        posting_url=url,  # the Remote OK URL, as their terms require
        apply_url=None,
        published_at=parse_iso(record.get("date")),
        tags=[t for t in record.get("tags") or [] if isinstance(t, str)],
    )


def parse_response(payload: Any, report: FetchReport) -> list[RawJob]:
    return parse_records(payload, parse_job, report)


class RemoteOkSource:
    name = "remoteok"

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        try:
            result = await ctx.http.get_json(API, ctx.policy)
        except (SourceAuthError, CapReachedError):
            raise
        except FetchError as exc:
            ctx.report.errors.append(str(exc))
            return []
        if result.not_modified:
            ctx.report.not_modified.append(result.url)
            return []
        try:
            return parse_response(result.data, ctx.report)
        except (KeyError, TypeError, ValueError) as exc:
            ctx.report.errors.append(f"unexpected response shape ({exc})")
            return []
