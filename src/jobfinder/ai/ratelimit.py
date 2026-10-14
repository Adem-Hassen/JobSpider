"""Client-side tracking of provider rate-limit headers (x-ratelimit-*, retry-after)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: str) -> float | None:
    """Parse '2m59.56s', '7.66s', '1h2m', '250ms' or plain seconds into seconds."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    parts = _DURATION_PART.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    return sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Seconds requested by retry-after-ms or retry-after (delta-seconds form only)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if "retry-after-ms" in lowered:
        try:
            return max(0.0, float(lowered["retry-after-ms"]) / 1000)
        except ValueError:
            pass
    if "retry-after" in lowered:
        try:
            return max(0.0, float(lowered["retry-after"]))
        except ValueError:
            return None
    return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


@dataclass
class RateLimitTracker:
    """Remembers the last rate-limit headers seen and says how long to wait before a call.

    Times are monotonic seconds supplied by the caller. One tracker per client instance.
    """

    remaining_requests: int | None = None
    requests_reset_at: float | None = None
    remaining_tokens: int | None = None
    tokens_reset_at: float | None = None

    def update(self, headers: Mapping[str, str], now: float) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}
        requests = _int(lowered.get("x-ratelimit-remaining-requests"))
        if requests is not None:
            self.remaining_requests = requests
            reset = parse_duration(lowered.get("x-ratelimit-reset-requests", ""))
            self.requests_reset_at = None if reset is None else now + reset
        tokens = _int(lowered.get("x-ratelimit-remaining-tokens"))
        if tokens is not None:
            self.remaining_tokens = tokens
            reset = parse_duration(lowered.get("x-ratelimit-reset-tokens", ""))
            self.tokens_reset_at = None if reset is None else now + reset

    def delay_before(self, estimated_tokens: int, now: float) -> float:
        """Seconds to wait so that a call needing ``estimated_tokens`` will not hit a limit."""
        delay = 0.0
        if (
            self.remaining_requests is not None
            and self.remaining_requests <= 0
            and self.requests_reset_at is not None
        ):
            delay = max(delay, self.requests_reset_at - now)
        if (
            self.remaining_tokens is not None
            and self.remaining_tokens < estimated_tokens
            and self.tokens_reset_at is not None
        ):
            delay = max(delay, self.tokens_reset_at - now)
        return max(0.0, delay)
