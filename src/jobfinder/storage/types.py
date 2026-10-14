"""Column types shared by the state store and the opportunity tables."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, String, TypeDecorator
from sqlalchemy.types import TypeEngine


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes.

    SQLite: fixed-width ISO-8601 text, which sorts chronologically.
    PostgreSQL: ``timestamp with time zone``. Naive datetimes are rejected.
    """

    impl = String(32)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(String(32))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime passed to storage; use timezone-aware UTC")
        utc = value.astimezone(UTC)
        if dialect.name == "postgresql":
            return utc
        return utc.isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        return datetime.fromisoformat(str(value)).astimezone(UTC)
