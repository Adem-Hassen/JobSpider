"""``jobfinder eval``: compare models against labelled jobs."""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from jobfinder.ai.client import ChatResult, LLMClient
from jobfinder.ai.errors import AIOutputError, AIProviderError
from jobfinder.ai.jobtext import prepare_job_text
from jobfinder.ai.ledger import SpendLedger
from jobfinder.ai.scoring import SCORE_OUTPUT, JobView, build_system_prompt, build_user_prompt
from jobfinder.ai.structured import structured_completion
from jobfinder.config import ProviderConfig, Settings
from jobfinder.models import Decision, ProfileRecord, ScoreResult, decision_for
from jobfinder.pipeline.text import html_to_text

LABELS = frozenset({"strong", "possible", "reject"})
REQUIRED_COLUMNS = ("title", "description", "label")
_BAND = {Decision.REJECT: 0, Decision.POSSIBLE: 1, Decision.STRONG: 2}


class EvalInputError(Exception):
    pass


@dataclass(frozen=True)
class LabelledJob:
    title: str
    company: str
    location: str
    description: str
    label: Decision

    def view(self) -> JobView:
        text = html_to_text(self.description) if "<" in self.description else self.description
        return JobView(
            title=self.title,
            company=self.company or "not stated",
            location=self.location or "not stated",
            remote_type="unknown",
            employment_type="not stated",
            published="unknown",
            text=prepare_job_text(text),
        )


def load_labels(path: Path, max_rows: int | None = None) -> list[LabelledJob]:
    """CSV with columns title, description, label (strong|possible|reject) and optionally
    company and location."""
    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise EvalInputError(f"{path}: {exc.strerror}") from exc
    with handle:
        reader = csv.DictReader(handle)
        columns = {c.strip().lower() for c in reader.fieldnames or []}
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise EvalInputError(f"{path.name}: missing column(s): {', '.join(missing)}")
        jobs = []
        for line, row in enumerate(reader, start=2):
            data = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            label = data["label"].lower()
            if label not in LABELS:
                raise EvalInputError(f"{path.name}:{line}: label must be one of {sorted(LABELS)}")
            if not data["title"] or not data["description"]:
                raise EvalInputError(f"{path.name}:{line}: title and description are required")
            jobs.append(
                LabelledJob(
                    title=data["title"],
                    company=data.get("company", ""),
                    location=data.get("location", ""),
                    description=data["description"],
                    label=Decision(label),
                )
            )
            if max_rows is not None and len(jobs) >= max_rows:
                break
    if not jobs:
        raise EvalInputError(f"{path.name}: no rows")
    return jobs


@dataclass
class ModelReport:
    provider: str
    model: str
    rows: int = 0
    exact: int = 0
    within_one: int = 0
    invalid_json: int = 0
    needed_retry: int = 0
    provider_errors: int = 0
    total_latency_s: float = 0.0
    total_cost_usd: float = 0.0
    skipped: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return self.rows - self.invalid_json - self.provider_errors

    @property
    def agreement(self) -> float:
        return self.exact / self.answered if self.answered else 0.0

    @property
    def agreement_within_one(self) -> float:
        return self.within_one / self.answered if self.answered else 0.0

    @property
    def json_failure_rate(self) -> float:
        attempted = self.rows - self.provider_errors
        return self.invalid_json / attempted if attempted else 0.0

    @property
    def avg_latency_s(self) -> float:
        attempted = self.rows - self.provider_errors
        return self.total_latency_s / attempted if attempted else 0.0

    @property
    def avg_cost_usd(self) -> float:
        attempted = self.rows - self.provider_errors
        return self.total_cost_usd / attempted if attempted else 0.0

    def projected_monthly_usd(self, jobs_per_day: float) -> float:
        return self.avg_cost_usd * jobs_per_day * 30


@dataclass
class _EvalRecorder:
    ledger: SpendLedger
    calls: list[ChatResult] = field(default_factory=list)

    def record(self, result: ChatResult, purpose: str) -> float:
        self.calls.append(result)
        return self.ledger.record(result, "eval")


async def evaluate_model(
    client: LLMClient,
    provider: ProviderConfig,
    model: str,
    profile: ProfileRecord,
    settings: Settings,
    jobs: Sequence[LabelledJob],
    ledger: SpendLedger,
    monotonic: Callable[[], float] = time.monotonic,
) -> ModelReport:
    """Score every job with ``model``; no cache and no budget cap (spend is recorded)."""
    report = ModelReport(provider=client.provider_name, model=model)
    system = build_system_prompt(profile, settings)
    for job in jobs:
        report.rows += 1
        recorder = _EvalRecorder(ledger)
        started = monotonic()
        try:
            result = await structured_completion(
                client,
                purpose="score",
                model=model,
                system=system,
                user=build_user_prompt(job.view()),
                output_model=ScoreResult,
                output=SCORE_OUTPUT,
                recorder=recorder,
            )
        except AIOutputError as exc:
            report.invalid_json += 1
            report.errors.append(f"{job.title}: {exc}")
        except AIProviderError as exc:
            report.provider_errors += 1
            report.errors.append(f"{job.title}: {exc}")
            if report.provider_errors >= 3 and report.answered == 0:
                report.skipped = f"stopped after repeated provider errors: {exc}"
                break
            continue
        else:
            predicted = decision_for(result.value.relevance_score)
            report.exact += predicted is job.label
            report.within_one += abs(_BAND[predicted] - _BAND[job.label]) <= 1
        finally:
            report.total_latency_s += monotonic() - started
            report.total_cost_usd += sum(ledger.cost_of(c) for c in recorder.calls)
        report.needed_retry += len(recorder.calls) > 1
    return report


def typical_jobs_per_day(
    spend_summary: Callable[[datetime, datetime, str], tuple[int, float]], now: datetime
) -> float | None:
    """Average daily scoring calls over the last 30 days, if there is any history."""
    count, _ = spend_summary(now - timedelta(days=30), now, "score")
    return count / 30 if count else None


def format_report(reports: Sequence[ModelReport], jobs_per_day: float) -> str:
    header = (
        f"{'model':<34} {'agree':>6} {'±1':>6} {'json fail':>9} {'retry':>6} "
        f"{'latency':>8} {'$/job':>9} {'$/month':>8}"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        name = f"{r.provider}/{r.model}"
        if r.skipped and r.answered == 0:
            lines.append(f"{name:<34} skipped: {r.skipped}")
            continue
        lines.append(
            f"{name:<34} {r.agreement:>6.0%} {r.agreement_within_one:>6.0%} "
            f"{r.json_failure_rate:>9.0%} {r.needed_retry:>6} {r.avg_latency_s:>7.1f}s "
            f"{r.avg_cost_usd:>9.5f} {r.projected_monthly_usd(jobs_per_day):>8.2f}"
        )
    lines.append(
        f"\nagree: exact label match; ±1: at most one band apart; json fail: invalid after "
        f"one retry; $/month: at {jobs_per_day:g} scored jobs per day."
    )
    return "\n".join(lines)
