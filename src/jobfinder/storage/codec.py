"""Conversions between Opportunity/Sighting models and flat storage rows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any

from jobfinder.models import OPPORTUNITY_COLUMNS, Opportunity, Sighting
from jobfinder.pipeline.normalise import norm_company
from jobfinder.storage.tables import LIST_COLUMNS


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def opportunity_to_sql(opportunity: Opportunity) -> dict[str, Any]:
    row = {name: _plain(getattr(opportunity, name)) for name in OPPORTUNITY_COLUMNS}
    for name in LIST_COLUMNS:
        row[name] = json.dumps(row[name], ensure_ascii=False)
    row["company_norm"] = norm_company(opportunity.company)
    return row


def opportunity_from_sql(row: Mapping[Any, Any]) -> Opportunity:
    data = {name: row[name] for name in OPPORTUNITY_COLUMNS}
    for name in LIST_COLUMNS:
        data[name] = json.loads(data[name]) if data[name] else []
    return Opportunity.model_validate(data)


def sighting_to_sql(sighting: Sighting) -> dict[str, Any]:
    return {name: _plain(getattr(sighting, name)) for name in Sighting.model_fields}


def sighting_from_sql(row: Mapping[Any, Any]) -> Sighting:
    return Sighting.model_validate({name: row[name] for name in Sighting.model_fields})
