from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobfinder.cli import app

runner = CliRunner()


def test_selfcheck_offline_ok(base_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["selfcheck", "--offline"])
    assert result.exit_code == 0, result.output
    assert "OK    state_db" in result.stdout
    assert "gsk_test_secret_value" not in result.output


def test_config_error_is_readable(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY")
    result = runner.invoke(app, ["selfcheck", "--offline"])
    assert result.exit_code == 1
    assert "GROQ_API_KEY: required when AI_PROVIDER=groq" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("args", [["storage", "migrate", "--to", "sqlite"]])
def test_unimplemented_commands_exit_2(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "not implemented yet" in result.stderr


def test_profile_show_without_profile(base_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 1
    assert "no profile yet" in result.stderr


def test_profile_import_unreadable_cv(base_env: dict[str, str], tmp_path: Path) -> None:
    cv = tmp_path / "cv.txt"
    cv.write_text("hello")
    result = runner.invoke(app, ["profile", "import", str(cv)])
    assert result.exit_code == 1
    assert "profile import failed: cv.txt: unsupported format .txt" in result.stderr


def test_profile_show_prints_latest(base_env: dict[str, str], data_dir: Path) -> None:
    from datetime import UTC, datetime

    from jobfinder.ai.examples import PROFILE_EXAMPLE
    from jobfinder.cv.profile_store import ProfileStore
    from jobfinder.models import ProfileRecord

    ProfileStore(data_dir / "profiles").save(
        ProfileRecord(
            version=1,
            created_at=datetime(2026, 10, 14, tzinfo=UTC),
            cv_sha256="c" * 64,
            cv_filename="cv.pdf",
            overrides_sha256="d" * 64,
            ai_provider="groq",
            ai_model="openai/gpt-oss-120b",
            extracted=PROFILE_EXAMPLE,
            profile=PROFILE_EXAMPLE,
        )
    )
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 0, result.output
    assert "# profile v1" in result.stderr
    assert '"seniority": "junior"' in result.stdout


def test_sources_test_unknown_name(base_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["sources", "test", "linkedin"])
    assert result.exit_code == 1
    assert "unknown source 'linkedin'" in result.stderr


def test_eval_needs_a_profile(base_env: dict[str, str], tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", "--labels", str(tmp_path / "x.csv")])
    assert result.exit_code == 1
    assert "no profile yet" in result.stderr


def _save_profile(data_dir: Path) -> None:
    from jobfinder.cv.profile_store import ProfileStore
    from tests.helpers import make_profile_record

    ProfileStore(data_dir / "profiles").save(make_profile_record())


def test_eval_rejects_bad_csv(base_env: dict[str, str], data_dir: Path, tmp_path: Path) -> None:
    _save_profile(data_dir)
    labels = tmp_path / "labels.csv"
    labels.write_text("title,description\nA,B\n")
    result = runner.invoke(app, ["eval", "--labels", str(labels)])
    assert result.exit_code == 1
    assert "missing column(s): label" in result.stderr


def test_eval_skips_models_without_key(
    base_env: dict[str, str], data_dir: Path, tmp_path: Path
) -> None:
    _save_profile(data_dir)
    labels = tmp_path / "labels.csv"
    labels.write_text("title,description,label\nPython Dev,Django APIs,strong\n")
    result = runner.invoke(
        app, ["eval", "--labels", str(labels), "--model", "deepseek/deepseek-flash"]
    )
    assert result.exit_code == 0, result.output
    assert "deepseek/deepseek-flash" in result.stdout
    assert "skipped: DEEPSEEK_API_KEY not set" in result.stdout


def test_run_skips_when_locked(base_env: dict[str, str], data_dir: Path) -> None:
    from jobfinder.locking import exclusive_lock

    with exclusive_lock(data_dir / "run.lock"):
        result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "skipped: another run is in progress" in result.stdout


@pytest.mark.parametrize("args", [["notify"], ["notify", "--failure", "--test"]])
def test_notify_needs_exactly_one_flag(base_env: dict[str, str], args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code == 2


def test_notify_without_telegram(base_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["notify", "--test"])
    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set" in result.stderr


def test_export_rejects_unknown_format(base_env: dict[str, str]) -> None:
    assert runner.invoke(app, ["export", "--format", "xml"]).exit_code == 2


def test_status_and_export_do_not_create_a_workbook(
    base_env: dict[str, str], data_dir: Path
) -> None:
    assert runner.invoke(app, ["status"]).exit_code == 0
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 0
    assert result.stdout.startswith("opportunity_id,")
    assert not (data_dir / "opportunities.xlsx").exists()
