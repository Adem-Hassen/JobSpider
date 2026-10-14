"""France Travail "Offres d'emploi v2" API (OAuth2 client credentials).

Token: POST https://entreprise.francetravail.fr/connexion/oauth2/access_token
       ?realm=/partenaire, cached until shortly before it expires.
Search: GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search
        with range=start-end (at most 150 per page); 200 = complete, 206 = more
        results, 204 = none. Rate limit: 10 calls per second.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobfinder.config import FranceTravailOptions, FranceTravailQuery
from jobfinder.http.client import CapReachedError, FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.sources.base import FetchContext, parse_iso, parse_records, text
from jobfinder.storage.state_store import SourceState

OAUTH_ENDPOINT = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
PAGE_SIZE = 150
TOKEN_MARGIN = timedelta(seconds=60)
_DEPARTMENT_PREFIX = re.compile(r"^\s*(?:\d{2,3}|2[AB])\s*-\s*")  # "75 - ", "974 - ", "2A - "
_ARRONDISSEMENT = re.compile(r"\s+\d{1,2}(?:e|er)?(?:\s+arrondissement)?\s*$", re.IGNORECASE)


def clean_place(libelle: str) -> str:
    """'75 - Paris 8e Arrondissement' -> 'Paris'; '69 - LYON 03' -> 'Lyon'."""
    place = _ARRONDISSEMENT.sub("", _DEPARTMENT_PREFIX.sub("", libelle)).strip()
    return place.title() if place.isupper() else place


def _employment_hint(record: Mapping[str, Any]) -> str | None:
    nature = (text(record.get("natureContrat")) or "").lower()
    if (
        record.get("alternance") is True
        or "apprentissage" in nature
        or "professionnalisation" in nature
    ):
        return "apprenticeship"
    return text(record.get("typeContrat"))  # CDI, CDD, MIS, SAI, LIB...


def parse_job(record: Mapping[str, Any]) -> RawJob | None:
    job_id = text(record.get("id"))
    title = text(record.get("intitule"))
    if job_id is None or title is None:
        return None
    origin = record.get("origineOffre") or {}
    url = text(origin.get("urlOrigine")) or (
        f"https://candidat.francetravail.fr/offres/recherche/detail/{job_id}"
    )
    place = text((record.get("lieuTravail") or {}).get("libelle"))
    contact = record.get("contact") or {}
    return RawJob(
        source="france_travail",
        source_job_id=job_id,
        is_ats=False,
        title=title,
        company=text((record.get("entreprise") or {}).get("nom")) or "Entreprise non communiquée",
        locations=[clean_place(place)] if place else [],
        country_hints=["FR"],
        employment_type_hint=_employment_hint(record),
        description_text=text(record.get("description")),
        posting_url=url,
        apply_url=text(contact.get("urlPostulation")),
        published_at=parse_iso(record.get("dateCreation")),
        tags=[t for t in [text(record.get("romeLibelle"))] if t],
    )


@dataclass
class TokenCache:
    """OAuth access token held in memory until shortly before it expires."""

    clock: Callable[[], datetime]
    token: str | None = None
    expires_at: datetime | None = None

    def valid(self) -> str | None:
        if self.token and self.expires_at and self.clock() < self.expires_at - TOKEN_MARGIN:
            return self.token
        return None

    def store(self, token: str, expires_in: float) -> None:
        self.token = token
        self.expires_at = self.clock() + timedelta(seconds=expires_in)


@dataclass
class FranceTravailSource:
    name: str = "france_travail"
    tokens: TokenCache | None = field(default=None)

    async def _token(self, ctx: FetchContext, scope: str) -> str:
        if self.tokens is None:
            self.tokens = TokenCache(ctx.http.clock)
        if (token := self.tokens.valid()) is not None:
            return token
        result = await ctx.http.post_form(
            OAUTH_ENDPOINT,
            ctx.policy,
            data={
                "grant_type": "client_credentials",
                "client_id": ctx.credentials.get("FRANCE_TRAVAIL_CLIENT_ID", ""),
                "client_secret": ctx.credentials.get("FRANCE_TRAVAIL_CLIENT_SECRET", ""),
                "scope": scope,
            },
            params={"realm": "/partenaire"},
        )
        data = result.data or {}
        token = text(data.get("access_token"))
        if token is None:
            raise SourceAuthError("france_travail: token response has no access_token", 401)
        self.tokens.store(token, float(data.get("expires_in") or 1499))
        return token

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        options = FranceTravailOptions.model_validate(ctx.options)
        token = await self._token(ctx, options.scope)
        jobs: list[RawJob] = []
        for raw_query in ctx.queries or [{}]:
            query = FranceTravailQuery.model_validate(raw_query)
            params = query.model_dump(exclude_none=True)
            for page in range(options.max_pages):
                start = page * PAGE_SIZE
                try:
                    result = await ctx.http.get_json(
                        SEARCH_URL,
                        ctx.policy,
                        params={**params, "range": f"{start}-{start + PAGE_SIZE - 1}"},
                        headers={"Authorization": f"Bearer {token}"},
                        conditional=False,
                    )
                except (SourceAuthError, CapReachedError):
                    if jobs:
                        ctx.report.errors.append("stopped early")
                        return jobs
                    raise
                except FetchError as exc:
                    ctx.report.errors.append(str(exc))
                    break
                if result.status_code == 204 or result.data is None:
                    break
                try:
                    jobs.extend(parse_records(result.data["resultats"], parse_job, ctx.report))
                except (KeyError, TypeError, ValueError) as exc:
                    ctx.report.errors.append(f"unexpected response shape ({exc})")
                    break
                if result.status_code != 206:  # 200: this was the last page
                    break
        return jobs
