"""Lever Postings API: GET /v0/postings/{site}?mode=json (public, no auth)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobfinder.config import AtsCompany
from jobfinder.models import RawJob
from jobfinder.pipeline.text import html_to_text
from jobfinder.sources.ats import AtsSource
from jobfinder.sources.base import parse_epoch_ms, text

API = {"global": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}


def _description(record: Mapping[str, Any]) -> str:
    parts = [text(record.get("descriptionPlain")) or ""]
    for section in record.get("lists") or []:
        heading = text(section.get("text"))
        body = text(section.get("content"))
        if heading:
            parts.append(heading)
        if body:
            parts.append(html_to_text(f"<ul>{body}</ul>"))
    parts.append(text(record.get("additionalPlain")) or "")
    return "\n\n".join(p for p in parts if p)


class LeverSource(AtsSource):
    name = "lever"

    def board_url(self, company: AtsCompany) -> str:
        return f"{API[company.region]}/{company.board_token}"

    def board_params(self, company: AtsCompany) -> Mapping[str, Any]:
        return {"mode": "json"}

    def records(self, payload: Any) -> Any:
        return payload

    def parse_job(self, record: Mapping[str, Any], company: AtsCompany) -> RawJob | None:
        url = text(record.get("hostedUrl"))
        title = text(record.get("text"))
        job_id = text(record.get("id"))
        if url is None or title is None or job_id is None:
            return None
        categories = record.get("categories") or {}
        locations = [loc for loc in categories.get("allLocations") or [] if text(loc)]
        if not locations and text(categories.get("location")):
            locations = [categories["location"]]
        country = text(record.get("country"))
        return RawJob(
            source=self.name,
            source_job_id=job_id,
            is_ats=True,
            title=title,
            company=company.name,
            locations=locations,
            country_hints=[country] if country else [],
            remote_hint=text(record.get("workplaceType")),
            employment_type_hint=text(categories.get("commitment")),
            description_text=_description(record) or None,
            posting_url=url,
            apply_url=text(record.get("applyUrl")),
            published_at=parse_epoch_ms(record.get("createdAt")),
            tags=[t for t in (categories.get("team"), categories.get("department")) if text(t)],
        )
