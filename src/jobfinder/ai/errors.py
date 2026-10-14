"""AI error types."""

from __future__ import annotations


class AIError(Exception):
    """Base class for AI failures."""


class AIProviderError(AIError):
    """The provider call failed (non-retryable status, or retries exhausted)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AIRateLimitedError(AIProviderError):
    """The provider asked us to wait longer than we are willing to block."""

    def __init__(self, message: str, retry_after_s: float | None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class AIOutputError(AIError):
    """The model's reply failed schema validation, including after one corrective retry."""
