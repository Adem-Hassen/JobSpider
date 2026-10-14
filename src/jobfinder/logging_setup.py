"""structlog configuration: one JSON object per line, UTC timestamps, secrets redacted.

Logs go to stderr so that command output on stdout stays clean; under systemd both
streams end up in the journal.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, TextIO

import structlog
from structlog.typing import FilteringBoundLogger

REDACTED = "***"
_SENSITIVE_KEYS = frozenset(
    {"api_key", "authorization", "password", "client_secret", "access_token", "secret"}
)


class SecretRedactor:
    """structlog processor that blanks sensitive keys and any known secret value."""

    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first so a secret containing another is replaced whole.
        self._secrets = sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, Mapping):
            return {
                k: REDACTED if str(k).lower() in _SENSITIVE_KEYS else self._scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self._scrub(v) for v in value]
        return value

    def __call__(
        self, _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for key in list(event_dict):
            if key.lower() in _SENSITIVE_KEYS:
                event_dict[key] = REDACTED
            else:
                event_dict[key] = self._scrub(event_dict[key])
        return event_dict


def configure_logging(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    stream: TextIO | None = None,
) -> None:
    """Configure structlog. Safe to call again once secrets are known."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.format_exc_info,
            SecretRedactor(secrets),
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    """A lazy logger: it uses whatever configuration is active when it first logs, so
    module-level loggers created at import time still follow configure_logging()."""
    logger: FilteringBoundLogger = structlog.get_logger(component=name)
    return logger
