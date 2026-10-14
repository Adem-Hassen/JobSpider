from __future__ import annotations

import pytest

from jobfinder.ai.ratelimit import RateLimitTracker, parse_duration, retry_after_seconds


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("2m59.56s", 179.56),
        ("7.66s", 7.66),
        ("620ms", 0.62),
        ("1h2m3s", 3723.0),
        ("12", 12.0),
        ("", None),
        ("soon", None),
        ("5x", None),
    ],
)
def test_parse_duration(text: str, seconds: float | None) -> None:
    result = parse_duration(text)
    if seconds is None:
        assert result is None
    else:
        assert result == pytest.approx(seconds)


def test_retry_after() -> None:
    assert retry_after_seconds({"Retry-After": "2"}) == 2.0
    assert retry_after_seconds({"retry-after-ms": "1500", "retry-after": "9"}) == 1.5
    assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert retry_after_seconds({}) is None


def test_tracker_waits_for_token_reset() -> None:
    tracker = RateLimitTracker()
    tracker.update(
        {
            "x-ratelimit-remaining-requests": "500",
            "x-ratelimit-reset-requests": "2m",
            "x-ratelimit-remaining-tokens": "300",
            "x-ratelimit-reset-tokens": "7.5s",
        },
        now=100.0,
    )
    assert tracker.delay_before(200, now=101.0) == 0.0
    assert tracker.delay_before(1000, now=101.0) == pytest.approx(6.5)
    assert tracker.delay_before(1000, now=200.0) == 0.0


def test_tracker_waits_when_requests_exhausted() -> None:
    tracker = RateLimitTracker()
    tracker.update(
        {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1m26.4s"},
        now=0.0,
    )
    assert tracker.delay_before(10, now=0.0) == pytest.approx(86.4)


def test_tracker_without_headers_never_waits() -> None:
    tracker = RateLimitTracker()
    tracker.update({"content-type": "application/json"}, now=0.0)
    assert tracker.delay_before(10**6, now=0.0) == 0.0
