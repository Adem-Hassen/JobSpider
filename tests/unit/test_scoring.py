from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.ai.budget import BudgetGuard
from jobfinder.ai.client import ChatRequest, ChatResult, TokenUsage
from jobfinder.ai.examples import SCORE_EXAMPLE
from jobfinder.ai.ledger import SpendLedger
from jobfinder.ai.scoring import (
    JobView,
    Scorer,
    ScoreStatus,
    apply_score,
    build_system_prompt,
    build_user_prompt,
    cache_key,
)
from jobfinder.config import AppConfig
from jobfinder.models import Decision, RemoteType, decision_for
from jobfinder.pipeline.normalise import normalise
from jobfinder.storage.state_store import SpendEntry, StateStore
from tests.helpers import make_profile_record, make_raw_job

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
JOB = JobView(
    "Junior Backend Developer",
    "Logistico",
    "Paris, FR",
    "hybrid",
    "full_time",
    "2026-10-12",
    "Python and Django.",
)


@dataclass
class FakeClient:
    replies: list[str] = field(default_factory=list)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def provider_name(self) -> str:
        return "groq"

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            self.replies.pop(0), "stop", TokenUsage(2000, 0, 400), "groq", request.model, NOW, 0.5
        )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StateStore]:
    with StateStore(tmp_path / "state.db") as s:
        yield s


def make_scorer(config: AppConfig, store: StateStore, client: FakeClient, **kw: object) -> Scorer:
    data: dict[str, object] = {
        "client": client,
        "provider": config.provider,
        "model": config.score_model,
        "profile": make_profile_record(),
        "settings": config.settings,
        "store": store,
        "ledger": SpendLedger(store, config.providers),
        "budget": BudgetGuard(store, 1.0, 20.0),
        "clock": lambda: NOW,
    }
    data.update(kw)
    return Scorer(**data)  # type: ignore[arg-type]


def test_system_prompt_is_stable_and_complete(app_config: AppConfig) -> None:
    first = build_system_prompt(make_profile_record(), app_config.settings)
    second = build_system_prompt(make_profile_record(), app_config.settings)
    assert first == second
    for expected in (
        "Skills (40)",
        "Role / title fit (20)",
        "Seniority (15)",
        "Location / eligibility (15)",
        "Employment type (10)",
        "keyword stuffing",
        '"seniority":"junior"',
        '"target_countries":["TN","FR","DE"]',
    ):
        assert expected in first


def test_user_prompt_delimits_posting() -> None:
    prompt = build_user_prompt(JOB)
    assert "<job>\nTitle: Junior Backend Developer\n" in prompt
    assert prompt.endswith("Python and Django.\n</job>")


def test_cache_key_depends_on_text_and_profile_version() -> None:
    user = build_user_prompt(JOB)
    assert cache_key(user, 1) == cache_key(user, 1)
    assert cache_key(user, 1) != cache_key(user, 2)
    assert cache_key(user, 1) != cache_key(user + " ", 1)


async def test_scores_then_serves_from_cache(app_config: AppConfig, store: StateStore) -> None:
    client = FakeClient([SCORE_EXAMPLE.model_dump_json()])
    scorer = make_scorer(app_config, store, client)
    first = await scorer.score(JOB)
    assert first.status is ScoreStatus.SCORED
    assert first.result == SCORE_EXAMPLE
    assert first.cost_usd == pytest.approx((2000 * 0.15 + 400 * 0.60) / 1e6)
    second = await scorer.score(JOB)
    assert second.status is ScoreStatus.CACHED
    assert second.result == SCORE_EXAMPLE
    assert second.cost_usd == 0
    assert len(client.requests) == 1
    assert store.spend_summary(NOW.replace(hour=0), NOW.replace(hour=23), "score") == (
        1,
        first.cost_usd,
    )


async def test_over_budget_makes_no_call(app_config: AppConfig, store: StateStore) -> None:
    store.record_spend(SpendEntry(NOW, "groq", "m", "score", 1, 0, 1, 0.9999))
    client = FakeClient([SCORE_EXAMPLE.model_dump_json()])
    outcome = await make_scorer(app_config, store, client).score(JOB)
    assert outcome.status is ScoreStatus.OVER_BUDGET
    assert client.requests == []


async def test_invalid_twice_is_ai_error_and_both_calls_paid(
    app_config: AppConfig, store: StateStore
) -> None:
    client = FakeClient(['{"relevance_score": "high"}', "not json"])
    outcome = await make_scorer(app_config, store, client).score(JOB)
    assert outcome.status is ScoreStatus.AI_ERROR
    assert len(outcome.calls) == 2
    assert outcome.cost_usd > 0
    assert store.get_ai_cache(cache_key(build_user_prompt(JOB), 1)) is None


async def test_summary_too_long_triggers_retry(app_config: AppConfig, store: StateStore) -> None:
    too_long = SCORE_EXAMPLE.model_dump() | {"summary": "x" * 301}
    client = FakeClient([json.dumps(too_long), SCORE_EXAMPLE.model_dump_json()])
    outcome = await make_scorer(app_config, store, client).score(JOB)
    assert outcome.status is ScoreStatus.SCORED
    assert "summary" in client.requests[1].messages[-1].content


@pytest.mark.parametrize(
    ("score", "decision"),
    [
        (100, Decision.STRONG),
        (75, Decision.STRONG),
        (74, Decision.POSSIBLE),
        (50, Decision.POSSIBLE),
        (49, Decision.REJECT),
    ],
)
def test_decision_bands(score: int, decision: Decision) -> None:
    assert decision_for(score) is decision


def test_apply_score(app_config: AppConfig) -> None:
    job = normalise(make_raw_job(remote_hint=None, employment_type_hint=None), ["FR"])
    opportunity = job.to_opportunity(NOW)
    result = SCORE_EXAMPLE.model_copy(
        update={"relevance_score": 80, "decision": "possible", "deadline": "2026-11-15"}
    )
    scored = apply_score(opportunity, result, 3, "openai/gpt-oss-120b")
    assert scored.decision is Decision.STRONG  # follows the score, not the model's label
    assert scored.relevance_score == 80
    assert scored.remote_type is RemoteType.HYBRID
    assert scored.employment_type is not None
    assert scored.deadline_at == datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC)
    assert (scored.profile_version, scored.ai_model) == (3, "openai/gpt-oss-120b")
    assert scored.matched_skills == SCORE_EXAMPLE.matched_skills
    assert scored.opportunity_id == opportunity.opportunity_id


def test_invalid_deadline_rejected() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        SCORE_EXAMPLE.model_validate(SCORE_EXAMPLE.model_dump() | {"deadline": "next Friday"})
