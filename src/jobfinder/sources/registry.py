"""All sources by name."""

from __future__ import annotations

from jobfinder.config import SourceName
from jobfinder.sources.adzuna import AdzunaSource
from jobfinder.sources.arbeitnow import ArbeitnowSource
from jobfinder.sources.ashby import AshbySource
from jobfinder.sources.base import Source
from jobfinder.sources.email_alerts import EmailAlertsSource
from jobfinder.sources.france_travail import FranceTravailSource
from jobfinder.sources.greenhouse import GreenhouseSource
from jobfinder.sources.himalayas import HimalayasSource
from jobfinder.sources.jobicy import JobicySource
from jobfinder.sources.lever import LeverSource
from jobfinder.sources.remoteok import RemoteOkSource
from jobfinder.sources.remotive import RemotiveSource


def implemented_sources() -> dict[SourceName, Source]:
    """Fresh source instances (some hold per-run state, e.g. an OAuth token)."""
    return {
        SourceName.GREENHOUSE: GreenhouseSource(),
        SourceName.LEVER: LeverSource(),
        SourceName.ASHBY: AshbySource(),
        SourceName.REMOTIVE: RemotiveSource(),
        SourceName.REMOTEOK: RemoteOkSource(),
        SourceName.JOBICY: JobicySource(),
        SourceName.HIMALAYAS: HimalayasSource(),
        SourceName.ARBEITNOW: ArbeitnowSource(),
        SourceName.ADZUNA: AdzunaSource(),
        SourceName.FRANCE_TRAVAIL: FranceTravailSource(),
        SourceName.EMAIL_ALERTS: EmailAlertsSource(),
    }
