"""ingest -> prefilter -> score, through the real OpenAI-compatible client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from jobfinder.ai.budget import BudgetGuard
from jobfinder.ai.client import OpenAICompatibleClient
from jobfinder.ai.ledger import SpendLedger
from jobfinder.ai.scoring import Scorer
from jobfinder.config import AppConfig, load_config
from jobfinder.models import Decision, Opportunity, RawJob
from jobfinder.pipeline.ingest import ingest
from jobfinder.pipeline.prefilter import Prefilter
from jobfinder.pipeline.score import ScoringStats, prefilter_new, score_pending
from jobfinder.storage.sql_repo import SqlRepository
from jobfinder.storage.state_store import SpendEntry, StateStore
from tests.helpers import (
    FakeSleep,
    ScriptedTransport,
    completion_body,
    make_profile_record,
    make_raw_job,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)  # Wednesday noon: DeepSeek off-peak
SCORE_FIXTURE = {"groq": "groq_score_response.json", "deepseek": "deepseek_score_response.json"}
SALES_JOB = {
    "source_job_id": "job-2",
    "title": "Account Executive",
    "description_text": "Sell our platform to enterprise customers.",
    "posting_url": "https://jobs.lever.co/logistico/job-2",
}


@pytest.fixture(params=["groq", "deepseek"])
def config(
    request: pytest.FixtureRequest, base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> AppConfig:
    if request.param == "deepseek":
        monkeypatch.setenv("AI_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    return load_config(env_file=None)


@pytest.fixture
def repo(config: AppConfig) -> Iterator[SqlRepository]:
    r = SqlRepository.sqlite(config.data_dir / "opportunities.db")
    yield r
    r.close()


class Harness:
    def __init__(self, config: AppConfig, repo: SqlRepository) -> None:
        self.config = config
        self.repo = repo
        self.sleep = FakeSleep()
        self.profile = make_profile_record()

    async def run(
        self, transport: ScriptedTransport, raw: list[RawJob], now: datetime = NOW
    ) -> ScoringStats:
        with StateStore(self.config.state_db_path) as store:
            ingested = ingest(raw, self.repo, preferred_countries=["FR"], now=now)
            prefilter_new(
                [(job, outcome.opportunity) for job, outcome in ingested.new],
                self.repo,
                store,
                Prefilter(self.profile.profile, self.config.settings),
                now,
            )
            async with OpenAICompatibleClient(
                name=self.config.ai_provider.value,
                provider=self.config.provider,
                api_key=self.config.ai_api_key.get_secret_value(),
                user_agent="jobfinder/test",
                http_client=transport.client(),
                sleep=self.sleep,
                clock=lambda: now,
            ) as client:
                scorer = Scorer(
                    client=client,
                    provider=self.config.provider,
                    model=self.config.score_model,
                    profile=self.profile,
                    settings=self.config.settings,
                    store=store,
                    ledger=SpendLedger(store, self.config.providers),
                    budget=BudgetGuard(
                        store,
                        self.config.settings.ai_daily_budget_usd,
                        self.config.settings.ai_monthly_budget_usd,
                    ),
                    clock=lambda: now,
                )
                return await score_pending(self.repo, store, scorer)

    def get(self, source_job_id: str) -> Opportunity:
        found = self.repo.get_by_source_key("lever", source_job_id)
        assert found is not None
        return found

    def spent(self, now: datetime = NOW) -> float:
        with StateStore(self.config.state_db_path) as store:
            return store.spend_between(now - timedelta(hours=12), now + timedelta(hours=12))


@pytest.fixture
def harness(config: AppConfig, repo: SqlRepository) -> Harness:
    return Harness(config, repo)


def _expected_cost(provider: str) -> float:
    if provider == "groq":
        return (2210 * 0.15 + 388 * 0.60) / 1e6
    return (476 * 0.66 + 2944 * 0.022 + 940 * 1.98) / 1e6  # off-peak v4-pro, cache hits


async def test_switching_provider_gives_the_same_result(harness: Harness) -> None:
    """Identical expectations for groq (strict json_schema) and deepseek (json_object)."""
    provider = harness.config.ai_provider.value
    transport = ScriptedTransport().add_fixture(SCORE_FIXTURE[provider])
    stats = await harness.run(transport, [make_raw_job(), make_raw_job(**SALES_JOB)])

    assert (stats.scored, stats.ai_errors, stats.provider_errors) == (1, 0, 0)
    assert len(transport.requests) == 1  # the sales job was filtered without an AI call

    scored = harness.get("job-1")
    assert scored.decision is Decision.STRONG
    assert scored.relevance_score == 82
    assert scored.matched_skills == ["Python", "Django", "PostgreSQL", "REST APIs"]
    assert scored.missing_skills == ["Kubernetes"]
    assert scored.deadline_at == datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC)
    assert scored.prefilter_score is not None and scored.prefilter_score >= 35
    assert (scored.ai_model, scored.profile_version) == (harness.config.score_model, 1)
    filtered = harness.get("job-2")
    assert filtered.decision is Decision.FILTERED
    assert filtered.score_reason is not None and filtered.score_reason.startswith("Filtered:")
    assert harness.spent() == pytest.approx(_expected_cost(provider))
    assert stats.cost_usd == pytest.approx(_expected_cost(provider))

    body = transport.bodies()[0]
    system = body["messages"][0]["content"]
    sent = transport.requests[0].content.decode()
    assert "Benefits" not in sent and "Free lunch" not in sent  # boilerplate trimmed
    assert body["reasoning_effort"] == "low"
    if provider == "groq":
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["response_format"]["json_schema"]["name"] == "job_score"
        assert "## Output format" not in system
    else:
        assert body["response_format"] == {"type": "json_object"}
        assert "## Output format" in system and "JSON Schema" in system


async def test_malformed_json_retried_then_ai_error(harness: Harness) -> None:
    transport = (
        ScriptedTransport()
        .add(200, completion_body('{"relevance_score": 82, "decision": "strong"'))
        .add(200, completion_body("Sure! Here is the JSON you asked for."))
    )
    stats = await harness.run(transport, [make_raw_job()])
    assert stats.ai_errors == 1
    assert len(transport.requests) == 2
    retry = transport.bodies()[1]["messages"]
    assert retry[-1]["role"] == "user" and "Your reply was invalid" in retry[-1]["content"]
    assert harness.get("job-1").decision is Decision.AI_ERROR
    assert harness.spent() > 0  # both failed calls were paid for


async def test_groq_429_retry_after_honoured(harness: Harness) -> None:
    if harness.config.ai_provider.value != "groq":
        pytest.skip("Groq-specific")
    transport = (
        ScriptedTransport()
        .add(429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "3"})
        .add_fixture(SCORE_FIXTURE["groq"])
    )
    stats = await harness.run(transport, [make_raw_job()])
    assert stats.scored == 1
    assert harness.sleep.calls == [3.0]
    assert harness.get("job-1").decision is Decision.STRONG


async def test_budget_exhaustion_leaves_pending_until_next_day(harness: Harness) -> None:
    with StateStore(harness.config.state_db_path) as store:
        store.record_spend(
            SpendEntry(NOW - timedelta(hours=1), "groq", "m", "score", 1, 0, 1, 0.9999)
        )
    transport = ScriptedTransport()
    stats = await harness.run(transport, [make_raw_job()])
    assert stats.over_budget == 1
    assert transport.requests == []
    assert harness.get("job-1").decision is Decision.PENDING_AI

    tomorrow = NOW + timedelta(days=1)
    provider = harness.config.ai_provider.value
    retry = ScriptedTransport().add_fixture(SCORE_FIXTURE[provider])
    stats = await harness.run(retry, [], now=tomorrow)
    assert stats.scored == 1
    assert harness.get("job-1").decision is Decision.STRONG


async def test_provider_failure_stops_scoring_and_keeps_jobs_pending(harness: Harness) -> None:
    second = make_raw_job(
        source_job_id="job-3",
        title="Python Developer",
        posting_url="https://jobs.lever.co/logistico/job-3",
    )
    transport = ScriptedTransport().add(401, {"error": {"message": "Invalid API Key"}})
    stats = await harness.run(transport, [make_raw_job(), second])
    assert stats.provider_errors == 1
    assert "401" in stats.errors[0]
    assert len(transport.requests) == 1
    assert {harness.get(i).decision for i in ("job-1", "job-3")} == {Decision.PENDING_AI}


async def test_rescoring_same_posting_uses_cache(harness: Harness) -> None:
    """A job back in pending_ai (e.g. after restoring an older workbook) costs nothing."""
    provider = harness.config.ai_provider.value
    await harness.run(ScriptedTransport().add_fixture(SCORE_FIXTURE[provider]), [make_raw_job()])
    spent = harness.spent()
    job = harness.get("job-1")
    harness.repo.upsert(job.model_copy(update={"decision": Decision.PENDING_AI}))
    with StateStore(harness.config.state_db_path) as store:
        from jobfinder.ai.jobtext import prepare_job_text

        store.put_job_text(
            job.opportunity_id, prepare_job_text(make_raw_job().description_text or ""), NOW
        )
    empty = ScriptedTransport()
    stats = await harness.run(empty, [])
    assert (stats.cached, stats.scored) == (1, 0)
    assert empty.requests == []
    assert harness.get("job-1").decision is Decision.STRONG
    assert harness.spent() == spent


def _groq_schema_rejection(generated: dict[str, object]) -> dict[str, object]:
    """Groq's reply when the model's JSON fails its own schema check (seen in production)."""
    return {
        "error": {
            "message": "Generated JSON does not match the expected schema. Please adjust your "
            "prompt. See 'failed_generation' for more details. Error: jsonschema: '' does not "
            "validate with /required: missing properties: 'required_skills'",
            "type": "invalid_request_error",
            "code": "json_validate_failed",
            "failed_generation": json.dumps(generated),
        }
    }


def _score_without_required_skills() -> dict[str, object]:
    from tests.helpers import load_ai_fixture

    content = json.loads(
        load_ai_fixture("groq_score_response.json")["body"]["choices"][0]["message"]["content"]
    )
    content.pop("required_skills")
    return content


async def test_groq_schema_rejection_is_retried_with_the_error(harness: Harness) -> None:
    if harness.config.ai_provider.value != "groq":
        pytest.skip("Groq-specific")
    transport = (
        ScriptedTransport()
        .add(400, _groq_schema_rejection(_score_without_required_skills()))
        .add_fixture(SCORE_FIXTURE["groq"])
    )
    stats = await harness.run(transport, [make_raw_job()])
    assert (stats.scored, stats.ai_errors, stats.provider_errors) == (1, 0, 0)
    retry = transport.bodies()[1]["messages"]
    assert "required_skills: Field required" in retry[-1]["content"]
    assert "use null or [] when unknown" in retry[-1]["content"]
    assert harness.get("job-1").decision is Decision.STRONG


async def test_bad_request_affects_only_that_job(harness: Harness) -> None:
    if harness.config.ai_provider.value != "groq":
        pytest.skip("Groq-specific")
    second = make_raw_job(
        source_job_id="job-3",
        title="Python Developer",
        posting_url="https://jobs.lever.co/logistico/job-3",
    )
    rejection = _groq_schema_rejection(_score_without_required_skills())
    transport = (
        ScriptedTransport()
        .add(400, rejection)  # first job: rejected twice -> ai_error
        .add(400, rejection)
        .add_fixture(SCORE_FIXTURE["groq"])  # scoring continues with the next job
    )
    stats = await harness.run(transport, [make_raw_job(), second])
    assert (stats.scored, stats.ai_errors, stats.provider_errors) == (1, 1, 0)
    decisions = {harness.get(i).decision for i in ("job-1", "job-3")}
    assert decisions == {Decision.AI_ERROR, Decision.STRONG}


async def test_other_400_marks_the_job_and_continues(harness: Harness) -> None:
    if harness.config.ai_provider.value != "groq":
        pytest.skip("Groq-specific")
    second = make_raw_job(
        source_job_id="job-3",
        title="Python Developer",
        posting_url="https://jobs.lever.co/logistico/job-3",
    )
    too_long = {
        "error": {
            "message": "Please reduce the length of the messages",
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
    }
    transport = ScriptedTransport().add(400, too_long).add_fixture(SCORE_FIXTURE["groq"])
    stats = await harness.run(transport, [make_raw_job(), second])
    assert (stats.scored, stats.ai_errors, stats.provider_errors) == (1, 1, 0)
