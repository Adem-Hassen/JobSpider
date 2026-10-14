"""Pipeline stages after dedupe: prefilter new jobs, then score pending ones."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jobfinder.ai.jobtext import prepare_job_text
from jobfinder.ai.scoring import JobView, Scorer, ScoreStatus, apply_score
from jobfinder.models import Decision, Opportunity
from jobfinder.pipeline.normalise import NormalisedJob
from jobfinder.pipeline.prefilter import Prefilter
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.state_store import StateStore


@dataclass
class PrefilterStats:
    passed: int = 0
    filtered: int = 0


def prefilter_new(
    new_jobs: list[tuple[NormalisedJob, Opportunity]],
    repo: OpportunityRepository,
    store: StateStore,
    prefilter: Prefilter,
    now: datetime,
) -> PrefilterStats:
    """Mark each new job ``filtered`` or ``pending_ai``; keep the text of pending ones."""
    stats = PrefilterStats()
    for job, opportunity in new_jobs:
        result = prefilter.evaluate(job, now)
        if result.passed:
            store.put_job_text(opportunity.opportunity_id, prepare_job_text(job.description), now)
            updated = opportunity.model_copy(
                update={"prefilter_score": result.score, "decision": Decision.PENDING_AI}
            )
            stats.passed += 1
        else:
            updated = opportunity.model_copy(
                update={
                    "prefilter_score": result.score,
                    "decision": Decision.FILTERED,
                    "score_reason": "Filtered: " + "; ".join(result.reasons),
                }
            )
            stats.filtered += 1
        repo.upsert(updated)
    return stats


@dataclass
class ScoringStats:
    scored: int = 0
    cached: int = 0
    over_budget: int = 0
    ai_errors: int = 0
    provider_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    newly_decided: list[Opportunity] = field(default_factory=list)


async def score_pending(
    repo: OpportunityRepository,
    store: StateStore,
    scorer: Scorer,
    limit: int | None = None,
) -> ScoringStats:
    """Score active ``pending_ai`` jobs, best prefilter score first.

    Stops at the first budget refusal or provider failure; the remaining jobs stay
    ``pending_ai`` and are retried on a later run (the budget resets daily).
    """
    stats = ScoringStats()
    pending = sorted(
        (o for o in repo.iter_active() if o.decision is Decision.PENDING_AI),
        key=lambda o: (-(o.prefilter_score or 0), o.discovered_at),
    )
    batch = pending[:limit] if limit is not None else pending
    for index, opportunity in enumerate(batch):
        text = store.get_job_text(opportunity.opportunity_id)
        if text is None:
            repo.upsert(
                opportunity.model_copy(
                    update={"decision": Decision.AI_ERROR, "score_reason": "Job text unavailable"}
                )
            )
            stats.ai_errors += 1
            continue
        outcome = await scorer.score(JobView.of(opportunity, text))
        stats.cost_usd += outcome.cost_usd
        for call in outcome.calls:
            stats.input_tokens += call.usage.input_tokens
            stats.output_tokens += call.usage.output_tokens
        if outcome.status is ScoreStatus.OVER_BUDGET:
            stats.over_budget = len(batch) - index
            break
        request_specific = (
            outcome.status is ScoreStatus.PROVIDER_ERROR and outcome.status_code == 400
        )
        if outcome.status is ScoreStatus.PROVIDER_ERROR and not request_specific:
            # Auth, rate limit, outage: later jobs would fail the same way.
            stats.provider_errors += 1
            stats.errors.append(outcome.error or "provider error")
            break
        if outcome.status is ScoreStatus.AI_ERROR or request_specific:
            stats.ai_errors += 1
            stats.errors.append(outcome.error or "invalid output")
            repo.upsert(
                opportunity.model_copy(
                    update={
                        "decision": Decision.AI_ERROR,
                        "ai_model": scorer.model,
                        "profile_version": scorer.profile.version,
                    }
                )
            )
            continue
        if outcome.result is None:  # pragma: no cover - SCORED/CACHED always carry one
            continue
        updated = apply_score(opportunity, outcome.result, scorer.profile.version, scorer.model)
        repo.upsert(updated)
        store.delete_job_text(opportunity.opportunity_id)
        stats.newly_decided.append(updated)
        if outcome.status is ScoreStatus.CACHED:
            stats.cached += 1
        else:
            stats.scored += 1
    return stats
