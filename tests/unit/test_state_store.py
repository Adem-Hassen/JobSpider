from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from jobfinder.storage.state_store import (
    SCHEMA_VERSION,
    HttpValidators,
    SourceState,
    SpendEntry,
    StateStore,
    StateStoreError,
    TokenBucketState,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    yield s  # type: ignore[misc]
    s.close()


def test_creates_schema_with_wal(store: StateStore) -> None:
    assert store.schema_version() == SCHEMA_VERSION
    assert store.journal_mode() == "wal"
    assert store.integrity_check() == "ok"


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with StateStore(path) as s:
        s.increment_counter("x", date(2026, 3, 1))
    with StateStore(path) as s:
        assert s.get_counter("x", date(2026, 3, 1)) == 1


def test_newer_schema_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    StateStore(path).close()
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("UPDATE meta SET value='999' WHERE key='schema_version'"))
    engine.dispose()
    with pytest.raises(StateStoreError, match="newer than this"):
        StateStore(path)


def test_source_state_default_and_roundtrip(store: StateStore) -> None:
    assert store.get_source_state("remotive") == SourceState(source="remotive")
    state = SourceState(
        source="remotive",
        last_attempt_at=T0,
        last_success_at=T0 - timedelta(hours=1),
        consecutive_failures=2,
        paused_until=T0 + timedelta(hours=24),
        pause_reason="3 failed runs",
        cursor="abc",
    )
    store.save_source_state(state)
    assert store.get_source_state("remotive") == state
    store.save_source_state(SourceState(source="remotive", consecutive_failures=0))
    assert store.get_source_state("remotive").paused_until is None
    assert [s.source for s in store.all_source_states()] == ["remotive"]


def test_datetimes_normalised_to_utc(store: StateStore) -> None:
    paris = timezone(timedelta(hours=2))
    local = datetime(2026, 3, 1, 14, 0, tzinfo=paris)
    store.save_source_state(SourceState(source="s", last_success_at=local))
    loaded = store.get_source_state("s").last_success_at
    assert loaded == local
    assert loaded is not None
    assert loaded.tzinfo == UTC
    assert loaded.hour == 12


def test_naive_datetime_rejected(store: StateStore) -> None:
    with pytest.raises(Exception, match="naive datetime"):
        store.save_source_state(SourceState(source="s", last_success_at=datetime(2026, 3, 1)))  # noqa: DTZ001


def test_http_validators(store: StateStore) -> None:
    url = "https://remotive.com/api/remote-jobs"
    assert store.get_http_validators(url) is None
    store.set_http_validators(url, HttpValidators('"v1"', None), T0)
    store.set_http_validators(url, HttpValidators('"v2"', "Sun, 01 Mar 2026 12:00:00 GMT"), T0)
    assert store.get_http_validators(url) == HttpValidators('"v2"', "Sun, 01 Mar 2026 12:00:00 GMT")


def test_counters_are_per_scope_and_day(store: StateStore) -> None:
    d1, d2 = date(2026, 3, 1), date(2026, 3, 2)
    assert store.increment_counter("source:adzuna", d1) == 1
    assert store.increment_counter("source:adzuna", d1, 4) == 5
    assert store.increment_counter("source:adzuna", d2) == 1
    assert store.increment_counter("source:remotive", d1) == 1
    assert store.get_counter("source:adzuna", d1) == 5
    assert store.get_counter("source:adzuna", date(2026, 3, 3)) == 0
    assert store.sum_counter("source:adzuna", d1, d2) == 6


def test_token_bucket_roundtrip(store: StateStore) -> None:
    assert store.get_token_bucket("host:remotive.com") is None
    store.set_token_bucket("host:remotive.com", TokenBucketState(1.5, T0))
    store.set_token_bucket("host:remotive.com", TokenBucketState(0.25, T0 + timedelta(seconds=5)))
    assert store.get_token_bucket("host:remotive.com") == TokenBucketState(
        0.25, T0 + timedelta(seconds=5)
    )


def test_ai_cache(store: StateStore) -> None:
    assert store.get_ai_cache("k") is None
    store.put_ai_cache("k", "m1", '{"a": 1}', T0)
    store.put_ai_cache("k", "m2", '{"a": 2}', T0)
    assert store.get_ai_cache("k") == '{"a": 2}'


def _spend(at: datetime, cost: float) -> SpendEntry:
    return SpendEntry(at, "groq", "openai/gpt-oss-120b", "score", 1000, 0, 200, cost)


def test_spend_between_is_half_open(store: StateStore) -> None:
    day = datetime(2026, 3, 1, tzinfo=UTC)
    store.record_spend(_spend(day - timedelta(microseconds=1), 1.0))
    store.record_spend(_spend(day, 0.25))
    store.record_spend(_spend(day + timedelta(hours=23, minutes=59), 0.5))
    store.record_spend(_spend(day + timedelta(days=1), 2.0))
    assert store.spend_between(day, day + timedelta(days=1)) == pytest.approx(0.75)
    assert store.spend_between(day + timedelta(days=5), day + timedelta(days=6)) == 0.0


def test_runs(store: StateStore) -> None:
    store.start_run("r1", T0)
    store.start_run("r2", T0 + timedelta(hours=1))
    store.finish_run("r1", T0 + timedelta(minutes=2), "success", '{"new": 3}')
    runs = store.recent_runs()
    assert [r.run_id for r in runs] == ["r2", "r1"]
    assert runs[1].status == "success"
    assert runs[1].stats_json == '{"new": 3}'
    assert runs[0].finished_at is None
    with pytest.raises(StateStoreError, match="unknown run_id"):
        store.finish_run("nope", T0, "success", "{}")


def test_job_texts(store: StateStore) -> None:
    assert store.get_job_text("o1") is None
    store.put_job_text("o1", "first", T0)
    store.put_job_text("o1", "second", T0)
    assert store.get_job_text("o1") == "second"
    store.delete_job_text("o1")
    assert store.get_job_text("o1") is None


def test_v1_database_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    StateStore(path).close()
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE job_texts"))
        conn.execute(text("UPDATE meta SET value='1' WHERE key='schema_version'"))
    engine.dispose()
    with StateStore(path) as s:
        assert s.schema_version() == SCHEMA_VERSION
        s.put_job_text("o1", "x", T0)
