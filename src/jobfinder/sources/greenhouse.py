"""Greenhouse Job Board API: GET /v1/boards/{token}/jobs?content=true (public, no auth)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobfinder.config import AtsCompany
from jobfinder.models import RawJob
from jobfinder.sources.ats import AtsSource
from jobfinder.sources.base import parse_iso, text

API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseSource(AtsSource):
    name = "greenhouse"

    def board_url(self, company: AtsCompany) -> str:
        return f"{API}/{company.board_token}/jobs"

    def board_params(self, company: AtsCompany) -> Mapping[str, Any]:
        return {"content": "true"}

    def records(self, payload: Any) -> Any:
        return payload["jobs"]

    def parse_job(self, record: Mapping[str, Any], company: AtsCompany) -> RawJob | None:
        url = text(record.get("absolute_url"))
        title = text(record.get("title"))
        if url is None or title is None or record.get("id") is None:
            return None
        location = text((record.get("location") or {}).get("name"))
        offices = [text(o.get("location") or o.get("name")) for o in record.get("offices") or []]
        locations = [location] if location else [o for o in offices if o]
        return RawJob(
            source=self.name,
            source_job_id=str(record["id"]),
            is_ats=True,
            title=title,
            company=text(record.get("company_name")) or company.name,
            locations=locations,
            description_html=text(record.get("content")),
            posting_url=url,
            apply_url=url,
            # first_published when given; updated_at is the closest available fallback.
            published_at=parse_iso(record.get("first_published"))
            or parse_iso(record.get("updated_at")),
            deadline_at=parse_iso(record.get("application_deadline")),
            tags=[d["name"] for d in record.get("departments") or [] if d.get("name")],
        )
