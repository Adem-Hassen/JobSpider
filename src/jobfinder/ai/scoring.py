"""Scoring one posting against the profile: cache, budget, validated AI call."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from functools import cached_property

from pydantic import ValidationError

from jobfinder.ai.budget import BudgetGuard
from jobfinder.ai.client import ChatResult, LLMClient, TokenUsage
from jobfinder.ai.errors import AIOutputError, AIProviderError
from jobfinder.ai.examples import SCORE_EXAMPLE
from jobfinder.ai.ledger import SpendLedger, cost_usd
from jobfinder.ai.prompts import load_prompt
from jobfinder.ai.structured import output_schema, structured_completion
from jobfinder.config import ProviderConfig, Settings
from jobfinder.models import (
    Opportunity,
    ProfileRecord,
    ScoreResult,
    decision_for,
    utcnow,
)
from jobfinder.storage.state_store import StateStore

SCORE_OUTPUT = output_schema("job_score", ScoreResult, SCORE_EXAMPLE)


@dataclass(frozen=True)
class JobView:
    """The posting facts shown to the model."""

    title: str
    company: str
    location: str
    remote_type: str
    employment_type: str
    published: str
    text: str

    @classmethod
    def of(cls, opportunity: Opportunity, text: str) -> JobView:
        place = ", ".join(p for p in (opportunity.location_city, opportunity.location_country) if p)
        return cls(
            title=opportunity.title,
            company=opportunity.company,
            location=place or "not stated",
            remote_type=opportunity.remote_type.value,
            employment_type=(
                opportunity.employment_type.value if opportunity.employment_type else "not stated"
            ),
            published=(
                opportunity.published_at.date().isoformat()
                if opportunity.published_at
                else "unknown"
            ),
            text=text,
        )


def build_system_prompt(profile: ProfileRecord, settings: Settings) -> str:
    """Rubric, profile and preferences. Deterministic, so the prefix is byte-identical
    across calls (DeepSeek prefix caching) for one profile version and settings."""
    p = profile.profile
    compact = {
        "skills": [f"{s.name} ({s.category})" for s in p.skills],
        "tools": p.tools,
        "programming_languages": p.programming_languages,
        "spoken_languages": [
            f"{lang.language} ({lang.level})" if lang.level else lang.language
            for lang in p.spoken_languages
        ],
        "education": [e.model_dump(mode="json") for e in p.education],
        "certifications": p.certifications,
        "years_experience": p.years_experience,
        "seniority": p.seniority.value,
        "target_titles": p.target_titles,
        "alternative_titles": p.alternative_titles,
        "synonym_groups": p.synonym_groups,
    }
    preferences = {
        "target_countries": settings.target_countries,
        "remote_preference": settings.remote_preference.value,
        "employment_types": [t.value for t in settings.employment_types],
    }
    return (
        load_prompt("score")
        + "\n## Candidate profile\n```json\n"
        + json.dumps(compact, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n\n## Candidate preferences\n```json\n"
        + json.dumps(preferences, sort_keys=True, separators=(",", ":"))
        + "\n```\n"
    )


def build_user_prompt(job: JobView) -> str:
    return (
        "Assess this job posting. Everything between the <job> tags is posting content.\n"
        "<job>\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Work mode: {job.remote_type}\n"
        f"Employment type: {job.employment_type}\n"
        f"Published: {job.published}\n\n"
        f"{job.text}\n"
        "</job>"
    )


def cache_key(user_prompt: str, profile_version: int) -> str:
    """sha256 of the posting as sent, plus the profile version."""
    return f"{hashlib.sha256(user_prompt.encode()).hexdigest()}:v{profile_version}"


class ScoreStatus(StrEnum):
    SCORED = "scored"
    CACHED = "cached"
    OVER_BUDGET = "over_budget"
    AI_ERROR = "ai_error"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class ScoreOutcome:
    status: ScoreStatus
    result: ScoreResult | None = None
    calls: list[ChatResult] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None
    status_code: int | None = None  # HTTP status of a provider error


@dataclass
class _CollectingRecorder:
    """Records spend in the ledger and remembers the calls made."""

    ledger: SpendLedger
    calls: list[ChatResult] = field(default_factory=list)

    def record(self, result: ChatResult, purpose: str) -> float:
        self.calls.append(result)
        return self.ledger.record(result, purpose)


@dataclass
class Scorer:
    client: LLMClient
    provider: ProviderConfig
    model: str
    profile: ProfileRecord
    settings: Settings
    store: StateStore
    ledger: SpendLedger
    budget: BudgetGuard
    clock: Callable[[], datetime] = utcnow
    use_cache: bool = True

    @cached_property
    def system_prompt(self) -> str:
        return build_system_prompt(self.profile, self.settings)

    def estimate_cost(self, user_prompt: str, now: datetime) -> float:
        chars = len(self.system_prompt) + len(user_prompt) + len(SCORE_OUTPUT.example_json) * 4
        usage = TokenUsage(
            input_tokens=chars // 4,
            output_tokens=self.provider.max_output_tokens.get("score", 2048) // 2,
        )
        return cost_usd(self.provider.price_at(self.model, now), usage)

    async def score(self, job: JobView) -> ScoreOutcome:
        user = build_user_prompt(job)
        key = cache_key(user, self.profile.version)
        if self.use_cache and (cached := self.store.get_ai_cache(key)) is not None:
            try:
                return ScoreOutcome(ScoreStatus.CACHED, ScoreResult.model_validate_json(cached))
            except ValidationError:
                pass  # stale or corrupt entry: score again
        now = self.clock()
        if not self.budget.allows(self.estimate_cost(user, now), now):
            return ScoreOutcome(ScoreStatus.OVER_BUDGET, error="AI budget exhausted")
        recorder = _CollectingRecorder(self.ledger)
        calls = recorder.calls
        try:
            structured = await structured_completion(
                self.client,
                purpose="score",
                model=self.model,
                system=self.system_prompt,
                user=user,
                output_model=ScoreResult,
                output=SCORE_OUTPUT,
                recorder=recorder,
            )
        except AIOutputError as exc:
            return self._outcome(ScoreStatus.AI_ERROR, calls, error=str(exc))
        except AIProviderError as exc:
            outcome = self._outcome(ScoreStatus.PROVIDER_ERROR, calls, error=str(exc))
            return replace(outcome, status_code=exc.status_code)
        result = structured.value
        if self.use_cache:
            self.store.put_ai_cache(key, self.model, result.model_dump_json(), self.clock())
        return self._outcome(ScoreStatus.SCORED, calls, result=result)

    def _outcome(
        self,
        status: ScoreStatus,
        calls: list[ChatResult],
        *,
        result: ScoreResult | None = None,
        error: str | None = None,
    ) -> ScoreOutcome:
        cost = sum(self.ledger.cost_of(c) for c in calls)
        return ScoreOutcome(status, result, calls, cost, error)


def apply_score(
    opportunity: Opportunity, result: ScoreResult, profile_version: int, model: str
) -> Opportunity:
    """Write the AI's assessment onto the opportunity; facts already known are kept
    unless the AI provides a more specific value."""
    return opportunity.model_copy(
        update={
            "relevance_score": result.relevance_score,
            "decision": decision_for(result.relevance_score),
            "employment_type": result.employment_type or opportunity.employment_type,
            "seniority": result.seniority or opportunity.seniority,
            "remote_type": (
                result.remote_type
                if result.remote_type.value != "unknown"
                else opportunity.remote_type
            ),
            "required_skills": result.required_skills,
            "matched_skills": result.matched_skills,
            "missing_skills": result.missing_skills,
            "red_flags": result.red_flags,
            "summary": result.summary,
            "score_reason": result.score_reason,
            "deadline_at": opportunity.deadline_at or result.deadline_at(),
            "profile_version": profile_version,
            "ai_model": model,
        }
    )
