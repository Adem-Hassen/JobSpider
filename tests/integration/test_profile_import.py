"""CV -> profile end to end, through the real client with recorded provider responses."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.ai.client import OpenAICompatibleClient
from jobfinder.ai.ledger import SpendLedger
from jobfinder.config import AppConfig, load_config
from jobfinder.cv.extract import CVError
from jobfinder.cv.importer import ImportOutcome, ImportStatus, import_cv
from jobfinder.cv.profile_store import ProfileStore
from jobfinder.models import ProfileOverrides, Seniority
from jobfinder.storage.state_store import StateStore
from tests.helpers import FakeSleep, ScriptedTransport, make_docx, make_pdf, sample_cv_lines

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)  # Wednesday, DeepSeek off-peak
FIXTURE = {"groq": "groq_profile_response.json", "deepseek": "deepseek_profile_response.json"}


@pytest.fixture(params=["groq", "deepseek"])
def provider_config(
    request: pytest.FixtureRequest, base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> AppConfig:
    if request.param == "deepseek":
        monkeypatch.setenv("AI_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    return load_config(env_file=None)


async def _import(
    config: AppConfig,
    cv: Path,
    transport: ScriptedTransport,
    overrides: ProfileOverrides | None = None,
) -> ImportOutcome:
    with StateStore(config.state_db_path) as state:
        async with OpenAICompatibleClient(
            name=config.ai_provider.value,
            provider=config.provider,
            api_key=config.ai_api_key.get_secret_value(),
            user_agent="jobfinder/test",
            http_client=transport.client(),
            sleep=FakeSleep(),
            clock=lambda: NOW,
        ) as client:
            return await import_cv(
                cv,
                store=ProfileStore(config.profiles_dir),
                cv_dir=config.cv_dir,
                overrides=overrides or config.profile_overrides,
                client=client,
                model=config.profile_model,
                recorder=SpendLedger(state, config.providers),
                now=NOW,
            )


async def test_import_creates_profile(provider_config: AppConfig, tmp_path: Path) -> None:
    config = provider_config
    cv = make_pdf(tmp_path / "Amira CV.pdf", sample_cv_lines())
    transport = ScriptedTransport().add_fixture(FIXTURE[config.ai_provider.value])

    outcome = await _import(config, cv, transport)

    assert outcome.status is ImportStatus.CREATED
    record = outcome.record
    assert record.version == 1
    assert record.ai_provider == config.ai_provider.value
    assert record.ai_model == config.profile_model
    assert record.profile.seniority is Seniority.JUNIOR
    assert "FastAPI" in record.profile.tools
    assert outcome.path == config.profiles_dir / "profile_v1.json"
    assert json.loads(outcome.path.read_text())["cv_filename"] == "Amira CV.pdf"
    assert len(list(config.cv_dir.glob("cv_*.pdf"))) == 1

    # Personal details never reach the provider.
    sent = transport.requests[0].content.decode()
    for secret in ("amira.bensalah@example.com", "98 765 432", "Habib Bourguiba"):
        assert secret not in sent
    assert "[EMAIL]" in sent

    # Spend recorded with the provider's usage.
    with StateStore(config.state_db_path) as state:
        spent = state.spend_between(NOW.replace(hour=0), NOW.replace(hour=23))
    assert spent > 0


async def test_deepseek_cost_uses_cache_and_off_peak(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    config = load_config(env_file=None)
    cv = make_pdf(tmp_path / "cv.pdf", sample_cv_lines())
    await _import(config, cv, ScriptedTransport().add_fixture(FIXTURE["deepseek"]))
    with StateStore(config.state_db_path) as state:
        spent = state.spend_between(NOW.replace(hour=0), NOW.replace(hour=23))
    # fixture usage: 2950 prompt (1024 cached), 1610 output; off-peak v4-pro rates
    expected = (1926 * 0.66 + 1024 * 0.022 + 1610 * 1.98) / 1e6
    assert spent == pytest.approx(expected)


async def test_unchanged_cv_is_noop(app_config: AppConfig, tmp_path: Path) -> None:
    cv = make_pdf(tmp_path / "cv.pdf", sample_cv_lines())
    await _import(app_config, cv, ScriptedTransport().add_fixture(FIXTURE["groq"]))

    empty = ScriptedTransport()  # any request would fail the test
    outcome = await _import(app_config, cv, empty)

    assert outcome.status is ImportStatus.UNCHANGED
    assert outcome.record.version == 1
    assert empty.requests == []
    assert ProfileStore(app_config.profiles_dir).versions() == [1]


async def test_changed_overrides_reapplied_without_ai(
    app_config: AppConfig, tmp_path: Path
) -> None:
    cv = make_pdf(tmp_path / "cv.pdf", sample_cv_lines())
    await _import(app_config, cv, ScriptedTransport().add_fixture(FIXTURE["groq"]))

    overrides = ProfileOverrides(target_titles=["Backend Engineer"], seniority=Seniority.MID)
    empty = ScriptedTransport()
    outcome = await _import(app_config, cv, empty, overrides)

    assert outcome.status is ImportStatus.OVERRIDES_REAPPLIED
    assert empty.requests == []
    record = outcome.record
    assert record.version == 2
    assert record.profile.target_titles == ["Backend Engineer"]
    assert record.profile.seniority is Seniority.MID
    assert record.extracted.target_titles != ["Backend Engineer"]  # AI output preserved
    assert record.profile.tools == record.extracted.tools  # untouched fields kept


async def test_new_cv_creates_new_version(app_config: AppConfig, tmp_path: Path) -> None:
    await _import(
        app_config,
        make_pdf(tmp_path / "cv.pdf", sample_cv_lines()),
        ScriptedTransport().add_fixture(FIXTURE["groq"]),
    )
    docx_cv = make_docx(tmp_path / "cv2.docx", [*sample_cv_lines(), "Kubernetes basics"])
    outcome = await _import(app_config, docx_cv, ScriptedTransport().add_fixture(FIXTURE["groq"]))
    assert outcome.status is ImportStatus.CREATED
    assert outcome.record.version == 2


async def test_unreadable_cv_makes_no_ai_call(app_config: AppConfig, tmp_path: Path) -> None:
    cv = make_pdf(tmp_path / "scan.pdf", ["Amira"])
    transport = ScriptedTransport()
    with pytest.raises(CVError):
        await _import(app_config, cv, transport)
    assert transport.requests == []
    assert ProfileStore(app_config.profiles_dir).versions() == []


async def test_invalid_ai_output_twice_saves_nothing(app_config: AppConfig, tmp_path: Path) -> None:
    from jobfinder.ai.errors import AIOutputError
    from tests.helpers import completion_body

    cv = make_pdf(tmp_path / "cv.pdf", sample_cv_lines())
    transport = (
        ScriptedTransport()
        .add(200, completion_body('{"skills": "lots"}'))
        .add(200, completion_body('{"skills": []}'))
    )
    with pytest.raises(AIOutputError):
        await _import(app_config, cv, transport)
    assert len(transport.requests) == 2
    assert ProfileStore(app_config.profiles_dir).versions() == []
