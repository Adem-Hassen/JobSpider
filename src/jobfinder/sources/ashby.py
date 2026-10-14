"""Ashby public job posting API: GET /posting-api/job-board/{name} (public, no auth)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from jobfinder.config import AtsCompany
from jobfinder.models import RawJob
from jobfinder.sources.ats import AtsSource
from jobfinder.sources.base import parse_iso, text

API = "https://api.ashbyhq.com/posting-api/job-board"


def _country(address: Any) -> str | None:
    if not isinstance(address, Mapping):
        return None
    postal = address.get("postalAddress") or {}
    return text(postal.get("addressCountry"))


def _job_id(record: Mapping[str, Any], url: str) -> str | None:
    """The documented API has no job id; use it when present, else the jobUrl's last part."""
    explicit = text(record.get("id"))
    if explicit:
        return explicit
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last or None


class AshbySource(AtsSource):
    name = "ashby"

    def board_url(self, company: AtsCompany) -> str:
        return f"{API}/{company.board_token}"

    def board_params(self, company: AtsCompany) -> Mapping[str, Any]:
        return {"includeCompensation": "false"}

    def records(self, payload: Any) -> Any:
        return payload["jobs"]

    def parse_job(self, record: Mapping[str, Any], company: AtsCompany) -> RawJob | None:
        if record.get("isListed") is False:
            return None  # unlisted jobs are meant to be reachable by direct link only
        url = text(record.get("jobUrl"))
        title = text(record.get("title"))
        if url is None or title is None:
            return None
        job_id = _job_id(record, url)
        if job_id is None:
            return None
        secondary = record.get("secondaryLocations") or []
        locations = [text(record.get("location"))] + [text(s.get("location")) for s in secondary]
        countries = [_country(record.get("address"))] + [
            _country(s.get("address")) for s in secondary
        ]
        remote_hint = text(record.get("workplaceType"))
        if remote_hint is None and record.get("isRemote") is True:
            remote_hint = "remote"
        return RawJob(
            source=self.name,
            source_job_id=job_id,
            is_ats=True,
            title=title,
            company=company.name,
            locations=[loc for loc in locations if loc],
            country_hints=[c for c in countries if c],
            remote_hint=remote_hint,
            employment_type_hint=text(record.get("employmentType")),
            description_text=text(record.get("descriptionPlain")),
            description_html=None
            if record.get("descriptionPlain")
            else text(record.get("descriptionHtml")),
            posting_url=url,
            apply_url=text(record.get("applyUrl")),
            published_at=parse_iso(record.get("publishedAt")),
            tags=[t for t in (record.get("department"), record.get("team")) if text(t)],
        )
