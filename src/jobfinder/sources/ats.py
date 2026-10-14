"""Shared behaviour of employer ATS boards: one request per configured company."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from jobfinder.config import AtsCompany
from jobfinder.http.client import CapReachedError, FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, FetchReport, parse_records
from jobfinder.storage.state_store import SourceState


class AtsSource:
    name: str = ""

    def board_url(self, company: AtsCompany) -> str:
        raise NotImplementedError

    def board_params(self, company: AtsCompany) -> Mapping[str, Any] | None:
        return None

    def records(self, payload: Any) -> Any:
        raise NotImplementedError

    def parse_job(self, record: Mapping[str, Any], company: AtsCompany) -> RawJob | None:
        raise NotImplementedError

    def parse_board(self, payload: Any, company: AtsCompany, report: FetchReport) -> list[RawJob]:
        return parse_records(self.records(payload), lambda r: self.parse_job(r, company), report)

    async def _fetch_board(self, ctx: FetchContext, company: AtsCompany) -> list[RawJob]:
        url = self.board_url(company)
        try:
            result = await ctx.http.get_json(url, ctx.policy, params=self.board_params(company))
        except (SourceAuthError, CapReachedError):
            raise
        except FetchError as exc:
            hint = " (check board_token)" if exc.status_code == 404 else ""
            ctx.report.errors.append(f"{company.name}: {exc}{hint}")
            return []
        if result.not_modified:
            ctx.report.not_modified.append(result.url)
            return []
        try:
            return self.parse_board(result.data, company, ctx.report)
        except (KeyError, TypeError, ValueError) as exc:
            ctx.report.errors.append(f"{company.name}: unexpected response shape ({exc})")
            return []

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        companies = [c for c in ctx.ats_companies if c.provider == self.name]
        boards = await asyncio.gather(*(self._fetch_board(ctx, c) for c in companies))
        return [job for board in boards for job in board]
