from __future__ import annotations

import io
import json
import time
from datetime import datetime

import pytest
from freezegun import freeze_time

from jobfinder.logging_setup import REDACTED, configure_logging, get_logger


def _capture(secrets: tuple[str, ...] = (), level: str = "INFO") -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level, secrets=secrets, stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@freeze_time("2026-03-01 12:00:00")
def test_json_lines_with_utc_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # local zone must not leak into timestamps
    time.tzset()
    stream = _capture()
    get_logger("test").info("hello", count=3)
    (line,) = _lines(stream)
    assert line["event"] == "hello"
    assert line["count"] == 3
    assert line["level"] == "info"
    assert line["component"] == "test"
    ts = datetime.fromisoformat(str(line["ts"]).replace("Z", "+00:00"))
    assert ts.utcoffset() is not None
    assert ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert ts.hour == 12


def test_level_filtering() -> None:
    stream = _capture(level="WARNING")
    log = get_logger("test")
    log.info("hidden")
    log.warning("shown")
    assert [line["event"] for line in _lines(stream)] == ["shown"]


def test_secret_values_redacted_everywhere() -> None:
    stream = _capture(secrets=("sk-very-secret",))
    get_logger("test").error(
        "request failed sk-very-secret",
        url="https://x.test/?key=sk-very-secret",
        nested={"headers": ["Bearer sk-very-secret"]},
    )
    raw = stream.getvalue()
    assert "sk-very-secret" not in raw
    (line,) = _lines(stream)
    assert line["url"] == f"https://x.test/?key={REDACTED}"


def test_sensitive_keys_redacted() -> None:
    stream = _capture()
    get_logger("test").info("x", password="hunter2", api_key="abc", tokens=1234)
    (line,) = _lines(stream)
    assert line["password"] == REDACTED
    assert line["api_key"] == REDACTED
    assert line["tokens"] == 1234  # AI token counts are not secrets


def test_short_values_not_treated_as_secrets() -> None:
    stream = _capture(secrets=("ab",))
    get_logger("test").info("about tabs")
    (line,) = _lines(stream)
    assert line["event"] == "about tabs"


def test_logger_created_before_configuration_still_emits_json() -> None:
    early = get_logger("early")  # e.g. a module-level logger created at import time
    stream = _capture()
    early.info("late")
    (line,) = _lines(stream)
    assert line["event"] == "late"
    assert line["component"] == "early"
