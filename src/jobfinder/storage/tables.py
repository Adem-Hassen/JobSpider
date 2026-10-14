"""SQLAlchemy Core tables for opportunities and sightings.

Column names are identical to the Opportunity/Sighting fields and to the Excel headers.
Types are portable (SQLite now, PostgreSQL-ready). ``company_norm`` is a derived,
indexed column used for fuzzy-candidate lookups.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from jobfinder.storage.types import UTCDateTime

metadata = MetaData()

opportunities = Table(
    "opportunities",
    metadata,
    Column("opportunity_id", String(26), primary_key=True),
    Column("fingerprint", String(40), nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("source", String(32), nullable=False),
    Column("source_job_id", String(255), nullable=False),
    Column("title", Text, nullable=False),
    Column("company", Text, nullable=False),
    Column("location_city", Text),
    Column("location_country", String(2)),
    Column("remote_type", String(16), nullable=False),
    Column("employment_type", String(32)),
    Column("seniority", String(16)),
    Column("required_skills", Text, nullable=False),  # JSON array
    Column("summary", Text),
    Column("posting_url", Text, nullable=False),
    Column("apply_url", Text),
    Column("apply_url_final", Text),
    Column("published_at", UTCDateTime),
    Column("deadline_at", UTCDateTime),
    Column("discovered_at", UTCDateTime, nullable=False),
    Column("last_seen_at", UTCDateTime, nullable=False),
    Column("listing_status", String(16), nullable=False),
    Column("relevance_score", Integer),
    Column("prefilter_score", Integer),
    Column("decision", String(16), nullable=False),
    Column("matched_skills", Text, nullable=False),  # JSON array
    Column("missing_skills", Text, nullable=False),  # JSON array
    Column("score_reason", Text),
    Column("red_flags", Text, nullable=False),  # JSON array
    Column("profile_version", Integer),
    Column("ai_model", String(128)),
    Column("is_possible_duplicate", Boolean, nullable=False),
    Column("duplicate_of", String(26)),
    Column("application_status", Text),
    Column("notes", Text),
    Column("company_norm", Text, nullable=False),
    UniqueConstraint("source", "source_job_id", name="uq_opportunities_source_key"),
    Index("ix_opportunities_canonical_url", "canonical_url"),
    Index("ix_opportunities_fingerprint", "fingerprint"),
    Index("ix_opportunities_company_norm", "company_norm"),
    Index("ix_opportunities_listing_status", "listing_status"),
)

sightings = Table(
    "sightings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("opportunity_id", String(26), nullable=False, index=True),
    Column("source", String(32), nullable=False),
    Column("source_job_id", String(255), nullable=False),
    Column("posting_url", Text, nullable=False),
    Column("canonical_url", Text, nullable=False, index=True),
    Column("seen_at", UTCDateTime, nullable=False),
    Column("match_rule", String(16), nullable=False),
    UniqueConstraint("source", "source_job_id", name="uq_sightings_source_key"),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", String(26), primary_key=True),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("status", String(16), nullable=False),
    Column("duration_s", Float),
    Column("peak_rss_mb", Float),
    Column("sources", Text),
    Column("requests", Integer),
    Column("request_errors", Integer),
    Column("jobs_fetched", Integer),
    Column("new", Integer),
    Column("updated", Integer),
    Column("expired", Integer),
    Column("filtered", Integer),
    Column("scored", Integer),
    Column("pending_ai", Integer),
    Column("notified", Integer),
    Column("ai_input_tokens", Integer),
    Column("ai_output_tokens", Integer),
    Column("ai_cost_usd", Float),
    Column("errors", Text),
)

profile = Table(
    "profile",
    metadata,
    Column("field", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

LIST_COLUMNS = frozenset({"required_skills", "matched_skills", "missing_skills", "red_flags"})
