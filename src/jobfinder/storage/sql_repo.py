"""OpportunityRepository on SQLAlchemy Core (SQLite today, PostgreSQL-ready)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, insert, or_, select, text, update

from jobfinder.models import (
    USER_OWNED_FIELDS,
    Decision,
    ListingStatus,
    Opportunity,
    ProfileRecord,
    RunStats,
    Sighting,
    UserFields,
    profile_rows,
)
from jobfinder.storage.codec import (
    opportunity_from_sql,
    opportunity_to_sql,
    sighting_from_sql,
    sighting_to_sql,
)
from jobfinder.storage.tables import metadata, opportunities, profile, runs, sightings


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class SqlRepository:
    """Writes go straight to the database; ``flush`` is a no-op."""

    def __init__(self, url: str, visible_decisions: frozenset[Decision] | None = None) -> None:
        self._engine: Engine = create_engine(url)
        if self._engine.dialect.name == "sqlite":
            event.listen(self._engine, "connect", _sqlite_pragmas)
        metadata.create_all(self._engine)
        self._create_visible_view(visible_decisions)

    @classmethod
    def sqlite(
        cls, path: Path, visible_decisions: frozenset[Decision] | None = None
    ) -> SqlRepository:
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(f"sqlite:///{path}", visible_decisions)

    def _create_visible_view(self, decisions: frozenset[Decision] | None) -> None:
        """``visible_opportunities``: what the workbook's Opportunities sheet would show."""
        where = "1 = 1"
        if decisions is not None:
            values = ", ".join(f"'{d.value}'" for d in sorted(decisions)) or "''"
            where = (
                f"decision IN ({values}) AND (listing_status = 'active' "
                "OR application_status IS NOT NULL OR notes IS NOT NULL)"
            )
        with self._engine.begin() as conn:
            conn.execute(text("DROP VIEW IF EXISTS visible_opportunities"))
            # ``where`` is built from Decision enum values only, never from user input.
            create = (
                f"CREATE VIEW visible_opportunities AS SELECT * FROM opportunities WHERE {where}"  # noqa: S608
            )
            conn.execute(text(create))

    def _one(self, stmt: Any) -> Opportunity | None:
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return opportunity_from_sql(row) if row else None

    def _by_id(self, opportunity_id: str) -> Opportunity | None:
        return self._one(
            select(opportunities).where(opportunities.c.opportunity_id == opportunity_id)
        )

    def get_by_source_key(self, source: str, source_job_id: str) -> Opportunity | None:
        found = self._one(
            select(opportunities).where(
                opportunities.c.source == source, opportunities.c.source_job_id == source_job_id
            )
        )
        if found is not None:
            return found
        return self._one(
            select(opportunities)
            .join(sightings, sightings.c.opportunity_id == opportunities.c.opportunity_id)
            .where(sightings.c.source == source, sightings.c.source_job_id == source_job_id)
        )

    def get_by_canonical_url(self, url: str) -> Opportunity | None:
        sighted = select(sightings.c.opportunity_id).where(sightings.c.canonical_url == url)
        return self._one(
            select(opportunities)
            .where(
                or_(
                    opportunities.c.canonical_url == url,
                    opportunities.c.opportunity_id.in_(sighted),
                )
            )
            .order_by(opportunities.c.discovered_at)
        )

    def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        return self._one(
            select(opportunities)
            .where(opportunities.c.fingerprint == fingerprint)
            .order_by(opportunities.c.discovered_at)
        )

    def fuzzy_candidates(self, company_norm: str) -> list[Opportunity]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(opportunities).where(opportunities.c.company_norm == company_norm)
            ).mappings()
            return [opportunity_from_sql(r) for r in rows]

    def upsert(self, opportunity: Opportunity) -> None:
        row = opportunity_to_sql(opportunity)
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(opportunities.c.opportunity_id).where(
                    opportunities.c.opportunity_id == opportunity.opportunity_id
                )
            ).first()
            if exists is None:
                conn.execute(insert(opportunities).values(**row))
            else:
                values = {
                    k: v
                    for k, v in row.items()
                    if k != "opportunity_id" and k not in USER_OWNED_FIELDS
                }
                conn.execute(
                    update(opportunities)
                    .where(opportunities.c.opportunity_id == opportunity.opportunity_id)
                    .values(**values)
                )

    def add_sighting(self, sighting: Sighting) -> None:
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(sightings.c.id).where(
                    sightings.c.source == sighting.source,
                    sightings.c.source_job_id == sighting.source_job_id,
                )
            ).first()
            if exists is None:
                conn.execute(insert(sightings).values(**sighting_to_sql(sighting)))

    def mark_status(self, opportunity_id: str, status: ListingStatus) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(opportunities)
                .where(opportunities.c.opportunity_id == opportunity_id)
                .values(listing_status=status.value)
            )

    def iter_active(self) -> Iterator[Opportunity]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(opportunities)
                .where(opportunities.c.listing_status == ListingStatus.ACTIVE.value)
                .order_by(opportunities.c.discovered_at)
            ).mappings()
            for row in rows:
                yield opportunity_from_sql(row)

    def iter_all(self) -> Iterator[Opportunity]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(opportunities).order_by(opportunities.c.discovered_at)
            ).mappings()
            for row in rows:
                yield opportunity_from_sql(row)

    def user_fields(self, opportunity_id: str) -> UserFields | None:
        found = self._by_id(opportunity_id)
        if found is None:
            return None
        return UserFields(application_status=found.application_status, notes=found.notes)

    def sightings_for(self, opportunity_id: str) -> list[Sighting]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(sightings)
                .where(sightings.c.opportunity_id == opportunity_id)
                .order_by(sightings.c.id)
            ).mappings()
            return [sighting_from_sql(r) for r in rows]

    def count(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(opportunities)).scalar_one())

    def has_source(self, source: str) -> bool:
        with self._engine.connect() as conn:
            found = (
                conn.execute(
                    select(opportunities.c.opportunity_id)
                    .where(opportunities.c.source == source)
                    .limit(1)
                ).first()
                or conn.execute(
                    select(sightings.c.id).where(sightings.c.source == source).limit(1)
                ).first()
            )
        return found is not None

    def append_run(self, stats: RunStats) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(runs).values(**stats.row()))

    def write_profile(self, record: ProfileRecord) -> None:
        with self._engine.begin() as conn:
            conn.execute(profile.delete())
            conn.execute(
                insert(profile), [{"field": f, "value": v} for f, v in profile_rows(record)]
            )

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self._engine.dispose()
