"""Operational state in a dedicated SQLite file (state.db).

Holds per-source scheduling state, HTTP validators, request counters, token buckets,
the AI result cache, the AI spend ledger and run history. Opportunity data lives in
the repositories, not here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Connection,
    Date,
    Engine,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from jobfinder.storage.types import UTCDateTime

# v1: initial. v2: job_texts. v3: robots_cache. v4: link_cache, feed_absence,
# liveness. v5: notified. New tables are added by create_all; no data migration has
# been needed.
SCHEMA_VERSION = 5


class StateStoreError(Exception):
    """The state database is unusable (corrupt, or newer than this code)."""


metadata = MetaData()

meta_table = Table(
    "meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

source_state_table = Table(
    "source_state",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("last_attempt_at", UTCDateTime),
    Column("last_success_at", UTCDateTime),
    Column("consecutive_failures", Integer, nullable=False, default=0),
    Column("paused_until", UTCDateTime),
    Column("pause_reason", Text),
    Column("cursor", Text),
)

http_validators_table = Table(
    "http_validators",
    metadata,
    Column("url", Text, primary_key=True),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("updated_at", UTCDateTime, nullable=False),
)

counters_table = Table(
    "request_counters",
    metadata,
    Column("scope", String(128), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("count", Integer, nullable=False, default=0),
)

token_buckets_table = Table(
    "token_buckets",
    metadata,
    Column("key", String(255), primary_key=True),
    Column("tokens", Float, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

ai_cache_table = Table(
    "ai_cache",
    metadata,
    Column("cache_key", String(128), primary_key=True),
    Column("model", String(128), nullable=False),
    Column("result_json", Text, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
)

ai_spend_table = Table(
    "ai_spend",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False, index=True),
    Column("provider", String(32), nullable=False),
    Column("model", String(128), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("cached_input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
)

job_texts_table = Table(
    "job_texts",
    metadata,
    Column("opportunity_id", String(26), primary_key=True),
    Column("text", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

robots_table = Table(
    "robots_cache",
    metadata,
    Column("host", String(255), primary_key=True),
    Column("status", Integer, nullable=False),
    Column("body", Text, nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False),
)

link_cache_table = Table(
    "link_cache",
    metadata,
    Column("url", Text, primary_key=True),
    Column("final_url", Text),
    Column("status", Integer),
    Column("note", Text),
    Column("resolved_at", UTCDateTime, nullable=False),
)

feed_absence_table = Table(
    "feed_absence",
    metadata,
    Column("opportunity_id", String(26), primary_key=True),
    Column("misses", Integer, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

liveness_table = Table(
    "liveness",
    metadata,
    Column("opportunity_id", String(26), primary_key=True),
    Column("last_checked_at", UTCDateTime, nullable=False),
    Column("failures", Integer, nullable=False),
    Column("last_status", Integer),
)

notified_table = Table(
    "notified",
    metadata,
    Column("opportunity_id", String(26), primary_key=True),
    Column("sent_at", UTCDateTime, nullable=False),
)

runs_table = Table(
    "runs",
    metadata,
    Column("run_id", String(32), primary_key=True),
    Column("started_at", UTCDateTime, nullable=False, index=True),
    Column("finished_at", UTCDateTime),
    Column("status", String(16), nullable=False),
    Column("stats_json", Text),
)


@dataclass(frozen=True)
class SourceState:
    source: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    pause_reason: str | None = None
    cursor: str | None = None


@dataclass(frozen=True)
class HttpValidators:
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class TokenBucketState:
    tokens: float
    updated_at: datetime


@dataclass(frozen=True)
class SpendEntry:
    at: datetime
    provider: str
    model: str
    purpose: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class LinkRecord:
    final_url: str | None
    status: int | None
    note: str | None
    resolved_at: datetime


@dataclass(frozen=True)
class LivenessRecord:
    last_checked_at: datetime
    failures: int
    last_status: int | None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    stats_json: str | None


def _on_connect(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class StateStore:
    """Typed access to state.db. Use as a context manager or call ``close()``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(f"sqlite:///{path}")
        event.listen(self._engine, "connect", _on_connect)
        self._migrate()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.dispose()

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self._engine.begin() as conn:
            yield conn

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        with self._tx() as conn:
            metadata.create_all(conn)
            current = conn.execute(
                select(meta_table.c.value).where(meta_table.c.key == "schema_version")
            ).scalar_one_or_none()
            if current is None:
                conn.execute(
                    insert(meta_table).values(key="schema_version", value=str(SCHEMA_VERSION))
                )
            elif int(current) > SCHEMA_VERSION:
                raise StateStoreError(
                    f"{self.path} has schema version {current}, newer than this "
                    f"jobfinder ({SCHEMA_VERSION}); upgrade jobfinder or restore a backup"
                )
            elif int(current) < SCHEMA_VERSION:
                conn.execute(
                    update(meta_table)
                    .where(meta_table.c.key == "schema_version")
                    .values(value=str(SCHEMA_VERSION))
                )

    def schema_version(self) -> int:
        with self._tx() as conn:
            value = conn.execute(
                select(meta_table.c.value).where(meta_table.c.key == "schema_version")
            ).scalar_one()
        return int(value)

    def integrity_check(self) -> str:
        """Return SQLite's integrity_check verdict ("ok" when healthy)."""
        with self._tx() as conn:
            rows = conn.execute(text("PRAGMA integrity_check")).scalars().all()
        return "; ".join(str(r) for r in rows)

    def journal_mode(self) -> str:
        with self._tx() as conn:
            return str(conn.execute(text("PRAGMA journal_mode")).scalar_one())

    # ------------------------------------------------------------------ sources

    def get_source_state(self, source: str) -> SourceState:
        with self._tx() as conn:
            row = conn.execute(
                select(source_state_table).where(source_state_table.c.source == source)
            ).one_or_none()
        if row is None:
            return SourceState(source=source)
        return SourceState(
            source=row.source,
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
            consecutive_failures=row.consecutive_failures,
            paused_until=row.paused_until,
            pause_reason=row.pause_reason,
            cursor=row.cursor,
        )

    def save_source_state(self, state: SourceState) -> None:
        values = {
            "source": state.source,
            "last_attempt_at": state.last_attempt_at,
            "last_success_at": state.last_success_at,
            "consecutive_failures": state.consecutive_failures,
            "paused_until": state.paused_until,
            "pause_reason": state.pause_reason,
            "cursor": state.cursor,
        }
        stmt = sqlite_insert(source_state_table).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[source_state_table.c.source],
            set_={k: stmt.excluded[k] for k in values if k != "source"},
        )
        with self._tx() as conn:
            conn.execute(stmt)

    def all_source_states(self) -> list[SourceState]:
        with self._tx() as conn:
            names = conn.execute(select(source_state_table.c.source)).scalars().all()
        return [self.get_source_state(n) for n in sorted(names)]

    # ------------------------------------------------------------------ HTTP validators

    def get_http_validators(self, url: str) -> HttpValidators | None:
        with self._tx() as conn:
            row = conn.execute(
                select(http_validators_table).where(http_validators_table.c.url == url)
            ).one_or_none()
        if row is None:
            return None
        return HttpValidators(etag=row.etag, last_modified=row.last_modified)

    def set_http_validators(self, url: str, validators: HttpValidators, now: datetime) -> None:
        stmt = sqlite_insert(http_validators_table).values(
            url=url, etag=validators.etag, last_modified=validators.last_modified, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[http_validators_table.c.url],
            set_={
                "etag": stmt.excluded.etag,
                "last_modified": stmt.excluded.last_modified,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        with self._tx() as conn:
            conn.execute(stmt)

    # ------------------------------------------------------------------ counters

    def increment_counter(self, scope: str, day: date, amount: int = 1) -> int:
        """Atomically add ``amount`` to the (scope, UTC day) counter; return the new total."""
        stmt = sqlite_insert(counters_table).values(scope=scope, day=day, count=amount)
        stmt = stmt.on_conflict_do_update(
            index_elements=[counters_table.c.scope, counters_table.c.day],
            set_={"count": counters_table.c.count + stmt.excluded["count"]},
        )
        with self._tx() as conn:
            conn.execute(stmt)
            total = conn.execute(
                select(counters_table.c.count).where(
                    counters_table.c.scope == scope, counters_table.c.day == day
                )
            ).scalar_one()
        return int(total)

    def get_counter(self, scope: str, day: date) -> int:
        return self.sum_counter(scope, day, day)

    def sum_counter(self, scope: str, first_day: date, last_day: date) -> int:
        """Sum of a counter over an inclusive range of UTC days (weekly/monthly caps)."""
        with self._tx() as conn:
            total = conn.execute(
                select(func.coalesce(func.sum(counters_table.c.count), 0)).where(
                    counters_table.c.scope == scope,
                    counters_table.c.day >= first_day,
                    counters_table.c.day <= last_day,
                )
            ).scalar_one()
        return int(total)

    # ------------------------------------------------------------------ token buckets

    def get_token_bucket(self, key: str) -> TokenBucketState | None:
        with self._tx() as conn:
            row = conn.execute(
                select(token_buckets_table).where(token_buckets_table.c.key == key)
            ).one_or_none()
        if row is None:
            return None
        return TokenBucketState(tokens=row.tokens, updated_at=row.updated_at)

    def set_token_bucket(self, key: str, state: TokenBucketState) -> None:
        stmt = sqlite_insert(token_buckets_table).values(
            key=key, tokens=state.tokens, updated_at=state.updated_at
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[token_buckets_table.c.key],
            set_={"tokens": stmt.excluded.tokens, "updated_at": stmt.excluded.updated_at},
        )
        with self._tx() as conn:
            conn.execute(stmt)

    # ------------------------------------------------------------------ AI cache

    def get_ai_cache(self, cache_key: str) -> str | None:
        with self._tx() as conn:
            value = conn.execute(
                select(ai_cache_table.c.result_json).where(ai_cache_table.c.cache_key == cache_key)
            ).scalar_one_or_none()
        return None if value is None else str(value)

    def put_ai_cache(self, cache_key: str, model: str, result_json: str, now: datetime) -> None:
        stmt = sqlite_insert(ai_cache_table).values(
            cache_key=cache_key, model=model, result_json=result_json, created_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ai_cache_table.c.cache_key],
            set_={
                "model": stmt.excluded.model,
                "result_json": stmt.excluded.result_json,
                "created_at": stmt.excluded.created_at,
            },
        )
        with self._tx() as conn:
            conn.execute(stmt)

    # ------------------------------------------------------------------ AI spend

    def record_spend(self, entry: SpendEntry) -> None:
        with self._tx() as conn:
            conn.execute(
                insert(ai_spend_table).values(
                    at=entry.at,
                    provider=entry.provider,
                    model=entry.model,
                    purpose=entry.purpose,
                    input_tokens=entry.input_tokens,
                    cached_input_tokens=entry.cached_input_tokens,
                    output_tokens=entry.output_tokens,
                    cost_usd=entry.cost_usd,
                )
            )

    def spend_between(self, start: datetime, end: datetime) -> float:
        """Total AI spend in USD for ``start <= at < end``."""
        with self._tx() as conn:
            total = conn.execute(
                select(func.coalesce(func.sum(ai_spend_table.c.cost_usd), 0.0)).where(
                    # Fixed-width ISO-8601 UTC text sorts chronologically.
                    ai_spend_table.c.at >= start,
                    ai_spend_table.c.at < end,
                )
            ).scalar_one()
        return float(total)

    def spend_summary(self, start: datetime, end: datetime, purpose: str) -> tuple[int, float]:
        """(number of calls, total USD) for one purpose with ``start <= at < end``."""
        with self._tx() as conn:
            count, total = conn.execute(
                select(
                    func.count(ai_spend_table.c.id),
                    func.coalesce(func.sum(ai_spend_table.c.cost_usd), 0.0),
                ).where(
                    ai_spend_table.c.purpose == purpose,
                    ai_spend_table.c.at >= start,
                    ai_spend_table.c.at < end,
                )
            ).one()
        return int(count), float(total)

    # ------------------------------------------------------------------ job texts

    def put_job_text(self, opportunity_id: str, text_value: str, now: datetime) -> None:
        """The trimmed posting text sent to the AI, kept until the job is scored."""
        stmt = sqlite_insert(job_texts_table).values(
            opportunity_id=opportunity_id, text=text_value, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[job_texts_table.c.opportunity_id],
            set_={"text": stmt.excluded.text, "updated_at": stmt.excluded.updated_at},
        )
        with self._tx() as conn:
            conn.execute(stmt)

    def get_job_text(self, opportunity_id: str) -> str | None:
        with self._tx() as conn:
            value = conn.execute(
                select(job_texts_table.c.text).where(
                    job_texts_table.c.opportunity_id == opportunity_id
                )
            ).scalar_one_or_none()
        return None if value is None else str(value)

    def delete_job_text(self, opportunity_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                job_texts_table.delete().where(job_texts_table.c.opportunity_id == opportunity_id)
            )

    # ------------------------------------------------------------------ robots.txt

    def get_robots(self, host: str) -> tuple[int, str, datetime] | None:
        with self._tx() as conn:
            row = conn.execute(
                select(robots_table).where(robots_table.c.host == host)
            ).one_or_none()
        return None if row is None else (row.status, row.body, row.fetched_at)

    def put_robots(self, host: str, status: int, body: str, now: datetime) -> None:
        stmt = sqlite_insert(robots_table).values(
            host=host, status=status, body=body, fetched_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[robots_table.c.host],
            set_={
                "status": stmt.excluded.status,
                "body": stmt.excluded.body,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
        with self._tx() as conn:
            conn.execute(stmt)

    # ------------------------------------------------------------------ links

    def get_link(self, url: str) -> LinkRecord | None:
        with self._tx() as conn:
            row = conn.execute(
                select(link_cache_table).where(link_cache_table.c.url == url)
            ).one_or_none()
        if row is None:
            return None
        return LinkRecord(row.final_url, row.status, row.note, row.resolved_at)

    def put_link(self, url: str, record: LinkRecord) -> None:
        stmt = sqlite_insert(link_cache_table).values(
            url=url,
            final_url=record.final_url,
            status=record.status,
            note=record.note,
            resolved_at=record.resolved_at,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=[link_cache_table.c.url])
        with self._tx() as conn:
            conn.execute(stmt)

    # ------------------------------------------------------------------ liveness

    def get_absence(self, opportunity_id: str) -> int:
        with self._tx() as conn:
            value = conn.execute(
                select(feed_absence_table.c.misses).where(
                    feed_absence_table.c.opportunity_id == opportunity_id
                )
            ).scalar_one_or_none()
        return int(value or 0)

    def set_absence(self, opportunity_id: str, misses: int, now: datetime) -> None:
        with self._tx() as conn:
            if misses <= 0:
                conn.execute(
                    feed_absence_table.delete().where(
                        feed_absence_table.c.opportunity_id == opportunity_id
                    )
                )
                return
            stmt = sqlite_insert(feed_absence_table).values(
                opportunity_id=opportunity_id, misses=misses, updated_at=now
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[feed_absence_table.c.opportunity_id],
                    set_={"misses": stmt.excluded.misses, "updated_at": stmt.excluded.updated_at},
                )
            )

    def get_liveness(self, opportunity_id: str) -> LivenessRecord | None:
        with self._tx() as conn:
            row = conn.execute(
                select(liveness_table).where(liveness_table.c.opportunity_id == opportunity_id)
            ).one_or_none()
        if row is None:
            return None
        return LivenessRecord(row.last_checked_at, row.failures, row.last_status)

    def set_liveness(self, opportunity_id: str, record: LivenessRecord) -> None:
        stmt = sqlite_insert(liveness_table).values(
            opportunity_id=opportunity_id,
            last_checked_at=record.last_checked_at,
            failures=record.failures,
            last_status=record.last_status,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[liveness_table.c.opportunity_id],
            set_={
                "last_checked_at": stmt.excluded.last_checked_at,
                "failures": stmt.excluded.failures,
                "last_status": stmt.excluded.last_status,
            },
        )
        with self._tx() as conn:
            conn.execute(stmt)

    # ------------------------------------------------------------------ notifications

    def was_notified(self, opportunity_id: str) -> bool:
        with self._tx() as conn:
            return (
                conn.execute(
                    select(notified_table.c.opportunity_id).where(
                        notified_table.c.opportunity_id == opportunity_id
                    )
                ).first()
                is not None
            )

    def mark_notified(self, opportunity_id: str, now: datetime) -> None:
        stmt = sqlite_insert(notified_table).values(opportunity_id=opportunity_id, sent_at=now)
        with self._tx() as conn:
            conn.execute(
                stmt.on_conflict_do_nothing(index_elements=[notified_table.c.opportunity_id])
            )

    # ------------------------------------------------------------------ runs

    def start_run(self, run_id: str, started_at: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                insert(runs_table).values(run_id=run_id, started_at=started_at, status="running")
            )

    def finish_run(self, run_id: str, finished_at: datetime, status: str, stats_json: str) -> None:
        with self._tx() as conn:
            result = conn.execute(
                update(runs_table)
                .where(runs_table.c.run_id == run_id)
                .values(finished_at=finished_at, status=status, stats_json=stats_json)
            )
            if result.rowcount != 1:
                raise StateStoreError(f"unknown run_id {run_id!r}")

    def recent_runs(self, limit: int = 10) -> list[RunRecord]:
        with self._tx() as conn:
            rows = conn.execute(
                select(runs_table).order_by(runs_table.c.started_at.desc()).limit(limit)
            ).all()
        return [
            RunRecord(
                run_id=r.run_id,
                started_at=r.started_at,
                finished_at=r.finished_at,
                status=r.status,
                stats_json=r.stats_json,
            )
            for r in rows
        ]
