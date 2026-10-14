from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from jobfinder.ai.client import ChatResult, TokenUsage
from jobfinder.ai.ledger import SpendLedger, cost_usd
from jobfinder.config import AppConfig, Price
from jobfinder.storage.state_store import StateStore

# 2026-10-14 is a Wednesday, 2026-10-17 a Saturday.
WED_PEAK = datetime(2026, 10, 14, 2, 30, tzinfo=UTC)
WED_GAP = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)
WED_EDGE = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
SAT_PEAK_HOURS = datetime(2026, 10, 17, 2, 30, tzinfo=UTC)


def test_cost_formula() -> None:
    price = Price(input=1.0, cached_input=0.1, output=2.0)
    usage = TokenUsage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=500_000)
    assert cost_usd(price, usage) == pytest.approx(0.6 + 0.04 + 1.0)


@pytest.mark.parametrize(
    ("at", "peak"),
    [(WED_PEAK, True), (WED_GAP, False), (WED_EDGE, False), (SAT_PEAK_HOURS, False)],
)
def test_deepseek_peak_windows(app_config: AppConfig, at: datetime, peak: bool) -> None:
    deepseek = app_config.providers.providers["deepseek"]  # type: ignore[index]
    assert deepseek.is_peak(at) is peak
    expected = 1.32 if peak else 0.66
    assert deepseek.price_at("deepseek-v4-pro", at).input == expected


def test_peak_exempt_dates(app_config: AppConfig) -> None:
    deepseek = app_config.providers.providers["deepseek"]  # type: ignore[index]
    exempt = deepseek.model_copy(update={"peak_exempt_dates": [date(2026, 10, 14)]})
    assert exempt.is_peak(WED_PEAK) is False


def test_non_utc_input_converted(app_config: AppConfig) -> None:
    from datetime import timedelta, timezone

    deepseek = app_config.providers.providers["deepseek"]  # type: ignore[index]
    tunis = timezone(timedelta(hours=1))
    assert deepseek.is_peak(datetime(2026, 10, 14, 3, 30, tzinfo=tunis)) is True  # 02:30 UTC


def test_groq_has_flat_price(app_config: AppConfig) -> None:
    groq = app_config.provider
    assert groq.price_at("openai/gpt-oss-120b", WED_PEAK) == groq.price_at(
        "openai/gpt-oss-120b", SAT_PEAK_HOURS
    )


def test_ledger_records_spend(app_config: AppConfig, tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        ledger = SpendLedger(store, app_config.providers)
        result = ChatResult(
            content="{}",
            finish_reason="stop",
            usage=TokenUsage(2_000, 1_000, 1_000),
            provider="deepseek",
            model="deepseek-v4-pro",
            requested_at=WED_PEAK,
            latency_s=1.0,
        )
        cost = ledger.record(result, "profile")
        assert cost == pytest.approx((1000 * 1.32 + 1000 * 0.044 + 1000 * 3.96) / 1e6)
        day = datetime(2026, 10, 14, tzinfo=UTC)
        assert store.spend_between(day, datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(cost)
