from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.ai.client import ChatRequest, ChatResult, TokenUsage
from jobfinder.ai.errors import AIProviderError
from jobfinder.ai.eval import (
    EvalInputError,
    evaluate_model,
    format_report,
    load_labels,
    typical_jobs_per_day,
)
from jobfinder.ai.examples import SCORE_EXAMPLE
from jobfinder.ai.ledger import SpendLedger
from jobfinder.config import AppConfig
from jobfinder.models import Decision
from jobfinder.storage.state_store import StateStore
from tests.helpers import make_profile_record

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
CSV = """title,company,location,description,label
Junior Python Developer,Acme,Paris,Build Django APIs,strong
Account Executive,Acme,Berlin,Sell software,reject
Data Analyst,Acme,Tunis,<p>SQL dashboards</p>,possible
"""


def _reply(score: int) -> str:
    return json.dumps(SCORE_EXAMPLE.model_dump() | {"relevance_score": score})


@dataclass
class ScriptClient:
    replies: list[str | Exception]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def provider_name(self) -> str:
        return "groq"

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(reply, "stop", TokenUsage(1000, 0, 200), "groq", request.model, NOW, 0.4)


def test_load_labels(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(CSV)
    jobs = load_labels(path)
    assert [j.label for j in jobs] == [Decision.STRONG, Decision.REJECT, Decision.POSSIBLE]
    assert jobs[2].view().text == "SQL dashboards"
    assert len(load_labels(path, max_rows=2)) == 2


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("title,description\nA,B\n", r"missing column\(s\): label"),
        ("title,description,label\nA,B,maybe\n", "label must be one of"),
        ("title,description,label\n,B,strong\n", "title and description are required"),
        ("title,description,label\n", "no rows"),
    ],
)
def test_load_labels_errors(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(content)
    with pytest.raises(EvalInputError, match=message):
        load_labels(path)


async def test_evaluate_model_metrics(app_config: AppConfig, tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(CSV)
    jobs = load_labels(path)
    client = ScriptClient(
        [
            _reply(90),  # strong == strong
            "garbage",
            _reply(60),  # retried; possible vs reject: one band apart
            "garbage",
            "still garbage",  # invalid JSON after retry
        ]
    )
    ticks = iter(range(100))
    with StateStore(tmp_path / "state.db") as store:
        ledger = SpendLedger(store, app_config.providers)
        report = await evaluate_model(
            client,
            app_config.provider,
            "openai/gpt-oss-120b",
            make_profile_record(),
            app_config.settings,
            jobs,
            ledger,
            monotonic=lambda: float(next(ticks)),
        )
        assert store.spend_summary(NOW.replace(hour=0), NOW.replace(hour=23), "eval")[0] == 5
    assert report.rows == 3
    assert report.answered == 2
    assert report.agreement == 0.5
    assert report.agreement_within_one == 1.0
    assert report.json_failure_rate == pytest.approx(1 / 3)
    assert report.needed_retry == 2
    assert report.avg_latency_s == 1.0
    per_call = (1000 * 0.15 + 200 * 0.60) / 1e6
    assert report.avg_cost_usd == pytest.approx(5 * per_call / 3)
    assert report.projected_monthly_usd(10) == pytest.approx(report.avg_cost_usd * 300)
    table = format_report([report], 10)
    assert "groq/openai/gpt-oss-120b" in table
    assert "50%" in table


async def test_provider_errors_stop_early(app_config: AppConfig, tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(CSV + CSV.split("\n", 1)[1])
    client = ScriptClient([AIProviderError("down", 503)] * 6)
    with StateStore(tmp_path / "state.db") as store:
        report = await evaluate_model(
            client,
            app_config.provider,
            "openai/gpt-oss-120b",
            make_profile_record(),
            app_config.settings,
            load_labels(path),
            SpendLedger(store, app_config.providers),
        )
    assert report.provider_errors == 3
    assert report.skipped is not None
    assert "skipped" in format_report([report], 10)


def test_typical_jobs_per_day() -> None:
    assert typical_jobs_per_day(lambda s, e, p: (600, 1.0), NOW) == 20
    assert typical_jobs_per_day(lambda s, e, p: (0, 0.0), NOW) is None
