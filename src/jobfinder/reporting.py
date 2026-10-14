"""Text for ``jobfinder status`` and data for ``jobfinder export``."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import Enum
from typing import Any

from jobfinder.ai.budget import BudgetGuard
from jobfinder.config import AppConfig
from jobfinder.cv.profile_store import ProfileStore
from jobfinder.models import OPPORTUNITY_COLUMNS, Decision, ListingStatus, Opportunity, RunStats
from jobfinder.pipeline.schedule import plan
from jobfinder.sources.registry import implemented_sources
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.state_store import StateStore


def _when(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else "never"


def status_report(
    config: AppConfig, store: StateStore, repo: OpportunityRepository, now: datetime
) -> str:
    lines = ["Recent runs (UTC):"]
    runs = store.recent_runs(5)
    if not runs:
        lines.append("  none yet")
    for run in runs:
        detail = ""
        if run.stats_json:
            stats = RunStats.model_validate_json(run.stats_json)
            detail = (
                f"{stats.new:>4} new {stats.out_of_scope:>4} off-country "
                f"{stats.scored:>4} scored {stats.notified:>3} sent "
                f"${stats.ai_cost_usd:.4f} {stats.duration_s:>5.0f}s"
            )
            if stats.errors:
                detail += f"  ! {stats.errors[0][:80]}"
        lines.append(f"  {_when(run.started_at)}  {run.status:<8} {detail}")

    lines.append("\nSources:")
    width = max(len(n.value) for n in config.sources.sources)
    for decision in plan(config, store, implemented_sources(), now):
        state = store.get_source_state(decision.name.value)
        lines.append(
            f"  {decision.name.value:<{width}}  {decision.reason:<40.40}  "
            f"last ok {_when(state.last_success_at)}"
            + (f"  failures {state.consecutive_failures}" if state.consecutive_failures else "")
        )

    budget = BudgetGuard(
        store, config.settings.ai_daily_budget_usd, config.settings.ai_monthly_budget_usd
    ).status(now)
    lines.append(
        f"\nAI budget ({config.ai_provider.value}/{config.score_model}): "
        f"today ${budget.spent_today:.4f} of ${budget.daily_cap:.2f}, "
        f"this month ${budget.spent_this_month:.4f} of ${budget.monthly_cap:.2f}"
    )

    opportunities = list(_all(repo))
    active = [o for o in opportunities if o.listing_status is ListingStatus.ACTIVE]
    decisions = Counter(o.decision for o in active)
    listing = Counter(o.listing_status for o in opportunities)
    order = [Decision.STRONG, Decision.POSSIBLE, Decision.PENDING_AI, Decision.AI_ERROR,
             Decision.REJECT, Decision.FILTERED]  # fmt: skip
    lines.append(
        f"Opportunities: {len(active)} active ("
        + ", ".join(f"{d.value} {decisions[d]}" for d in order)
        + f"); {listing[ListingStatus.EXPIRED]} expired, "
        f"{listing[ListingStatus.INACCESSIBLE]} inaccessible"
    )
    profile = ProfileStore(config.profiles_dir).latest()
    lines.append(
        f"Profile: v{profile.version} from {profile.cv_filename} ({_when(profile.created_at)})"
        if profile
        else "Profile: none; run `jobfinder profile import <cv>`"
    )
    return "\n".join(lines)


def _all(repo: OpportunityRepository) -> Iterable[Opportunity]:
    return repo.iter_all()


# --------------------------------------------------------------------------- export

_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(_FORMULA):
        return "'" + value  # keep spreadsheet apps from evaluating fetched text
    return value


def select_for_export(
    repo: OpportunityRepository,
    *,
    include_all: bool,
    min_score: int | None,
    decisions: Iterable[Decision] = (Decision.STRONG, Decision.POSSIBLE),
) -> list[Opportunity]:
    if include_all:
        chosen = list(_all(repo))
    else:
        wanted = set(decisions)
        chosen = [o for o in repo.iter_active() if o.decision in wanted]
    if min_score is not None:
        chosen = [o for o in chosen if (o.relevance_score or 0) >= min_score]
    return sorted(chosen, key=lambda o: (-(o.relevance_score or -1), o.discovered_at))


def to_csv(opportunities: Sequence[Opportunity]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(OPPORTUNITY_COLUMNS)
    for o in opportunities:
        writer.writerow([_cell(getattr(o, name)) for name in OPPORTUNITY_COLUMNS])
    return buffer.getvalue()


def to_json(opportunities: Sequence[Opportunity]) -> str:
    return json.dumps(
        [o.model_dump(mode="json") for o in opportunities], ensure_ascii=False, indent=2
    )
