from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.config import AppConfig
from jobfinder.models import Decision, ListingStatus, RunStats
from jobfinder.reporting import select_for_export, status_report, to_csv, to_json
from jobfinder.storage.sql_repo import SqlRepository
from jobfinder.storage.state_store import StateStore
from tests.unit.test_repositories import make

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[SqlRepository]:
    r = SqlRepository.sqlite(tmp_path / "o.db")
    r.upsert(
        make(
            "01J0000000000000000000000A",
            source_job_id="1",
            decision=Decision.STRONG,
            relevance_score=90,
        )
    )
    r.upsert(
        make(
            "01J0000000000000000000000B",
            source_job_id="2",
            decision=Decision.POSSIBLE,
            relevance_score=60,
            title='=HYPERLINK("http://x")',
        )
    )
    r.upsert(make("01J0000000000000000000000C", source_job_id="3", decision=Decision.FILTERED))
    r.upsert(
        make(
            "01J0000000000000000000000D",
            source_job_id="4",
            decision=Decision.STRONG,
            relevance_score=95,
            listing_status=ListingStatus.EXPIRED,
        )
    )
    yield r
    r.close()


def test_default_export_is_active_strong_and_possible(repo: SqlRepository) -> None:
    chosen = select_for_export(repo, include_all=False, min_score=None)
    assert [o.source_job_id for o in chosen] == ["1", "2"]
    assert [o.source_job_id for o in select_for_export(repo, include_all=False, min_score=80)] == [
        "1"
    ]
    assert len(select_for_export(repo, include_all=True, min_score=None)) == 4


def test_csv_neutralises_formulas(repo: SqlRepository) -> None:
    rows = list(
        csv.DictReader(
            io.StringIO(to_csv(select_for_export(repo, include_all=False, min_score=None)))
        )
    )
    assert rows[1]["title"] == '\'=HYPERLINK("http://x")'
    assert rows[0]["required_skills"] == "Python; SQL"
    assert rows[0]["discovered_at"].startswith("2026-10-01T09:30:15")


def test_json_export(repo: SqlRepository) -> None:
    data = json.loads(to_json(select_for_export(repo, include_all=False, min_score=None)))
    assert data[1]["title"] == '=HYPERLINK("http://x")'  # JSON keeps the exact text


def test_status_report(app_config: AppConfig, repo: SqlRepository, tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        store.start_run("r1", T0)
        stats = RunStats(
            run_id="r1", started_at=T0, status="partial", new=5, errors=["source lever: boom"]
        )
        store.finish_run("r1", T0, "partial", stats.model_dump_json())
        text = status_report(app_config, store, repo, T0)
    assert "partial     5 new" in text
    assert "! source lever: boom" in text
    assert "greenhouse" in text and "due" in text
    assert "AI budget (groq/openai/gpt-oss-120b): today $0.0000 of $1.00" in text
    assert "Opportunities: 3 active (strong 1, possible 1" in text
    assert "1 expired" in text
    assert "Profile: none" in text
