from __future__ import annotations

from collections.abc import Callable
from datetime import time
from pathlib import Path
from typing import Any

import pytest

from jobfinder.config import (
    AIProvider,
    ConfigError,
    PeakWindow,
    SourceName,
    StorageBackend,
    load_config,
)

Edit = Callable[[str, Callable[[dict[str, Any]], None]], None]


def _problems(env_file: Path | None = None) -> list[str]:
    with pytest.raises(ConfigError) as info:
        load_config(env_file=env_file)
    return info.value.problems


def test_shipped_config_is_valid(base_env: dict[str, str]) -> None:
    config = load_config(env_file=None)
    assert config.ai_provider is AIProvider.GROQ
    assert config.score_model == "openai/gpt-oss-120b"
    assert config.profile_model == "openai/gpt-oss-120b"
    assert config.secrets.STORAGE_BACKEND is StorageBackend.EXCEL
    assert config.settings.prefilter_threshold == 35
    assert config.settings.notify_threshold == 80
    assert config.state_db_path == Path(base_env["DATA_DIR"]) / "state.db"
    assert SourceName.GREENHOUSE in config.sources.enabled()


def test_deepseek_defaults(base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    config = load_config(env_file=None)
    assert config.score_model == "deepseek-v4-pro"
    assert config.provider.base_url == "https://api.deepseek.com"
    assert config.ai_api_key.get_secret_value() == "sk-deepseek"


def test_model_override_must_be_known(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_MODEL_SCORE", "qwen/qwen3.8-27b")
    assert load_config(env_file=None).score_model == "qwen/qwen3.8-27b"
    monkeypatch.setenv("AI_MODEL_SCORE", "gpt-4o")
    assert any("AI_MODEL_SCORE='gpt-4o'" in p for p in _problems())


def test_missing_required_env_lists_every_problem(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBFINDER_CONFIG_DIR", str(config_dir))
    problems = _problems()
    assert "environment: AI_PROVIDER: required but not set" in problems
    assert "environment: CONTACT_EMAIL: required but not set" in problems


def test_missing_provider_key(base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY")
    assert _problems() == ["environment: GROQ_API_KEY: required when AI_PROVIDER=groq"]


def test_empty_env_value_counts_as_unset(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert any("GROQ_API_KEY" in p for p in _problems())


def test_invalid_enum_and_email(base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("CONTACT_EMAIL", "not-an-email")
    problems = _problems()
    assert any(p.startswith("environment: AI_PROVIDER:") for p in problems)
    assert any(p.startswith("environment: CONTACT_EMAIL:") for p in problems)


def test_env_file_is_read(config_dir: Path, data_dir: Path, tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"AI_PROVIDER=groq\nGROQ_API_KEY=k\nCONTACT_EMAIL=a@b.co\n"
        f"DATA_DIR={data_dir}\nJOBFINDER_CONFIG_DIR={config_dir}\n"
    )
    assert load_config(env_file=env_file).secrets.CONTACT_EMAIL == "a@b.co"


def test_settings_errors_are_readable(base_env: dict[str, str], edit_yaml: Edit) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["prefilter_threshold"] = 150
        d["target_countries"] = ["FR", "France"]
        d["employment_types"] = ["freelance"]
        d["typo_key"] = 1

    edit_yaml("settings.yaml", mutate)
    problems = _problems()
    assert any(p.startswith("settings.yaml: prefilter_threshold:") for p in problems)
    assert any(p.startswith("settings.yaml: target_countries") for p in problems)
    assert any(p.startswith("settings.yaml: employment_types.0:") for p in problems)
    assert "settings.yaml: typo_key: unknown key (check spelling)" in problems


def test_country_codes_are_uppercased(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml("settings.yaml", lambda d: d.update(target_countries=["fr", "de"]))
    assert load_config(env_file=None).settings.target_countries == ["FR", "DE"]


def test_notify_below_prefilter_rejected(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml("settings.yaml", lambda d: d.update(notify_threshold=20))
    assert any("notify_threshold (20) must be >=" in p for p in _problems())


def test_daily_budget_above_monthly_rejected(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml("settings.yaml", lambda d: d.update(ai_daily_budget_usd=50))
    assert any("ai_daily_budget_usd" in p for p in _problems())


def test_invalid_yaml_reported(base_env: dict[str, str], config_dir: Path) -> None:
    (config_dir / "settings.yaml").write_text("target_countries: [FR\n")
    assert any(p.startswith("settings.yaml: invalid YAML") for p in _problems())


def test_missing_file_reported(base_env: dict[str, str], config_dir: Path) -> None:
    (config_dir / "providers.yaml").unlink()
    assert any("providers.yaml: file not found" in p for p in _problems())


def test_missing_config_dir(base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBFINDER_CONFIG_DIR", "/nonexistent/jobfinder")
    assert any("is not a directory" in p for p in _problems())


def test_unknown_source_rejected(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml("sources.yaml", lambda d: d["sources"].update(linkedin={"enabled": True}))
    assert any(p.startswith("sources.yaml: sources.linkedin") for p in _problems())


@pytest.mark.parametrize(
    ("source", "change", "fragment"),
    [
        ("remotive", {"daily_request_cap": 5}, "exceeds the source's terms (4/day)"),
        ("remotive", {"requests_per_minute": {"remotive.com": 3}}, "(2/min)"),
        ("adzuna", {"daily_request_cap": 300}, "(250/day)"),
        ("adzuna", {"requests_per_minute": {"api.adzuna.com": 30}}, "(25/min)"),
        ("jobicy", {"min_interval_minutes": 30}, "below the source's terms (60)"),
    ],
)
def test_source_terms_are_ceilings(
    base_env: dict[str, str], edit_yaml: Edit, source: str, change: dict[str, Any], fragment: str
) -> None:
    edit_yaml("sources.yaml", lambda d: d["sources"][source].update(change))
    assert any(fragment in p for p in _problems())


def test_invalid_host_rejected(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml(
        "sources.yaml",
        lambda d: d["sources"]["greenhouse"].update(
            requests_per_minute={"https://boards-api.greenhouse.io": 10}
        ),
    )
    assert any("invalid host names" in p for p in _problems())


def test_enabled_source_requires_credentials(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml("sources.yaml", lambda d: d["sources"]["adzuna"].update(enabled=True))
    problems = _problems()
    assert "environment: ADZUNA_APP_ID: required because source adzuna is enabled" in problems
    assert "environment: ADZUNA_APP_KEY: required because source adzuna is enabled" in problems


def test_disabled_source_needs_no_credentials(base_env: dict[str, str]) -> None:
    load_config(env_file=None)  # adzuna, france_travail, email_alerts disabled, no creds


def test_duplicate_ats_company(base_env: dict[str, str], edit_yaml: Edit) -> None:
    company = {"name": "X", "provider": "greenhouse", "board_token": "GitLab"}
    edit_yaml("sources.yaml", lambda d: d["ats_companies"].append(company))
    assert any("duplicate greenhouse board_token" in p for p in _problems())


def test_telegram_values_must_be_paired(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert any("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in p for p in _problems())


def test_default_model_needs_price(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml(
        "providers.yaml",
        lambda d: d["providers"]["groq"]["default_models"].update(score="unpriced-model"),
    )
    assert any("default_models.score 'unpriced-model'" in p for p in _problems())


def test_off_peak_price_needs_window(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml("providers.yaml", lambda d: d["providers"]["deepseek"].update(peak_windows_utc=[]))
    assert any("peak_windows_utc is empty" in p for p in _problems())


def test_eval_model_must_be_priced(base_env: dict[str, str], edit_yaml: Edit) -> None:
    edit_yaml(
        "providers.yaml",
        lambda d: d["eval_models"].append({"provider": "groq", "model": "mystery"}),
    )
    assert any("groq/mystery has no price entry" in p for p in _problems())


def test_secret_values_are_hidden_in_repr(app_config: Any) -> None:
    assert "gsk_test_secret_value" not in repr(app_config.secrets)
    assert "gsk_test_secret_value" in app_config.secrets.secret_values()


def test_peak_window_must_not_wrap() -> None:
    with pytest.raises(ValueError, match="start must be before end"):
        PeakWindow(days=["mon"], start=time(22, 0), end=time(2, 0))  # type: ignore[list-item]


def test_overrides_validated(base_env: dict[str, str], config_dir: Path) -> None:
    (config_dir / "profile.overrides.yaml").write_text("seniority: wizard\ntarget_title: [x]\n")
    problems = _problems()
    assert any(p.startswith("profile.overrides.yaml: seniority:") for p in problems)
    assert "profile.overrides.yaml: target_title: unknown key (check spelling)" in problems


def test_overrides_loaded(base_env: dict[str, str], config_dir: Path) -> None:
    (config_dir / "profile.overrides.yaml").write_text("target_titles: [Backend Engineer]\n")
    overrides = load_config(env_file=None).profile_overrides
    assert overrides.target_titles == ["Backend Engineer"]
    assert overrides.model_dump(exclude_unset=True) == {"target_titles": ["Backend Engineer"]}


@pytest.mark.parametrize(
    ("source", "change", "fragment"),
    [
        ("adzuna", {"queries": [{"what": "python"}]}, "adzuna: queries.0.country: Field required"),
        ("adzuna", {"queries": [{"country": "France"}]}, "adzuna: queries.0.country"),
        ("remotive", {"queries": [{"category": "x", "bogus": 1}]}, "remotive: queries.0.bogus"),
        (
            "france_travail",
            {"queries": [{"publieeDepuis": 5}]},
            "france_travail: queries.0.publieeDepuis",
        ),
        ("himalayas", {"queries": [{"q": "python"}]}, "himalayas: does not take queries"),
        ("jobicy", {"options": {"max_pages": 0}}, "jobicy: options.max_pages"),
        ("greenhouse", {"options": {"x": 1}}, "greenhouse: options.x"),
        (
            "email_alerts",
            {"options": {"allowed_senders": ["news@example.org"]}},
            "no parser for news@example.org",
        ),
    ],
)
def test_source_queries_and_options_validated(
    base_env: dict[str, str], edit_yaml: Edit, source: str, change: dict[str, Any], fragment: str
) -> None:
    edit_yaml("sources.yaml", lambda d: d["sources"][source].update(change))
    assert any(fragment in p for p in _problems()), _problems()


def test_enabled_email_alerts_needs_senders(
    base_env: dict[str, str], edit_yaml: Edit, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.setenv(var, "x")
    edit_yaml(
        "sources.yaml", lambda d: d["sources"]["email_alerts"].update(enabled=True, options={})
    )
    assert any("allowed_senders must list at least one sender" in p for p in _problems())
