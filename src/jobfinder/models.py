"""Domain models."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def to_millis(value: datetime) -> datetime:
    """Drop sub-millisecond precision, the finest every storage backend (Excel) keeps."""
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def utcnow() -> datetime:
    return to_millis(datetime.now(UTC))


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return to_millis(value.astimezone(UTC))


UtcDatetime = Annotated[datetime, AfterValidator(_require_aware_utc)]


class SourceRunStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    requests: int = 0
    errors: int = 0
    jobs_fetched: int = 0


class RunStats(BaseModel):
    """Summary of one ``jobfinder run``, written to the Runs sheet and the log."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    status: str = "running"
    duration_s: float = 0.0
    peak_rss_mb: float = 0.0
    sources: dict[str, SourceRunStats] = Field(default_factory=dict)
    new: int = 0
    updated: int = 0
    out_of_scope: int = 0  # fetched but dropped: outside target_countries
    expired: int = 0
    inaccessible: int = 0
    filtered: int = 0
    scored: int = 0
    pending_ai: int = 0
    ai_errors: int = 0
    links_resolved: int = 0
    notified: int = 0
    ai_input_tokens: int = 0
    ai_output_tokens: int = 0
    ai_cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)

    def row(self) -> dict[str, object]:
        """Flat values for the Runs sheet/table."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "duration_s": round(self.duration_s, 1),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "sources": ", ".join(f"{name}:{s.status}" for name, s in sorted(self.sources.items())),
            "requests": sum(s.requests for s in self.sources.values()),
            "request_errors": sum(s.errors for s in self.sources.values()),
            "jobs_fetched": sum(s.jobs_fetched for s in self.sources.values()),
            "new": self.new,
            "updated": self.updated,
            "expired": self.expired,
            "filtered": self.filtered,
            "scored": self.scored,
            "pending_ai": self.pending_ai,
            "notified": self.notified,
            "ai_input_tokens": self.ai_input_tokens,
            "ai_output_tokens": self.ai_output_tokens,
            "ai_cost_usd": round(self.ai_cost_usd, 6),
            "errors": "; ".join(self.errors)[:2000],
        }


RUN_COLUMNS: tuple[str, ...] = (
    "run_id", "started_at", "finished_at", "status", "duration_s", "peak_rss_mb", "sources",
    "requests", "request_errors", "jobs_fetched", "new", "updated", "expired", "filtered",
    "scored", "pending_ai", "notified", "ai_input_tokens", "ai_output_tokens", "ai_cost_usd",
    "errors",
)  # fmt: skip
PROFILE_COLUMNS: tuple[str, ...] = ("field", "value")


# --------------------------------------------------------------------------- enums


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    APPRENTICESHIP = "apprenticeship"
    TEMPORARY = "temporary"


class RemoteType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class ListingStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    INACCESSIBLE = "inaccessible"


class Decision(StrEnum):
    STRONG = "strong"
    POSSIBLE = "possible"
    REJECT = "reject"
    AI_ERROR = "ai_error"
    PENDING_AI = "pending_ai"
    FILTERED = "filtered"


# --------------------------------------------------------------------------- profile


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"


class _AIModel(BaseModel):
    """Base for models the AI fills in: every field is required (nullable where optional)."""

    model_config = ConfigDict(extra="forbid")


class Skill(_AIModel):
    name: str = Field(description="Canonical skill name, e.g. 'PostgreSQL'.")
    category: Literal["technical", "domain", "soft"]
    evidence: str = Field(description="Short quote or paraphrase from the CV showing the skill.")


class SpokenLanguage(_AIModel):
    language: str
    level: str | None = Field(description="CEFR level or 'native' when stated, else null.")


class Education(_AIModel):
    degree: str
    field_of_study: str | None
    institution: str | None
    end_year: int | None = Field(description="Graduation year, or expected year; null if unknown.")


class Profile(_AIModel):
    skills: list[Skill]
    tools: list[str]
    programming_languages: list[str]
    spoken_languages: list[SpokenLanguage]
    education: list[Education]
    certifications: list[str]
    years_experience: float = Field(ge=0, description="Professional experience in years.")
    seniority: Seniority
    target_titles: list[str] = Field(description="Job titles the candidate is best suited for.")
    alternative_titles: list[str] = Field(description="Adjacent titles worth considering.")
    synonym_groups: list[list[str]] = Field(
        description="Groups of interchangeable terms, e.g. ['Postgres', 'PostgreSQL']."
    )
    include_keywords: list[str] = Field(description="Terms that signal a relevant posting.")
    exclude_keywords: list[str] = Field(description="Terms that signal an irrelevant posting.")


class ProfileOverrides(BaseModel):
    """profile.overrides.yaml: any Profile field; a value given here replaces the AI's."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skills: list[Skill] | None = None
    tools: list[str] | None = None
    programming_languages: list[str] | None = None
    spoken_languages: list[SpokenLanguage] | None = None
    education: list[Education] | None = None
    certifications: list[str] | None = None
    years_experience: float | None = Field(default=None, ge=0)
    seniority: Seniority | None = None
    target_titles: list[str] | None = None
    alternative_titles: list[str] | None = None
    synonym_groups: list[list[str]] | None = None
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None

    def apply(self, profile: Profile) -> Profile:
        update = self.model_dump(exclude_unset=True, mode="python")
        return Profile.model_validate({**profile.model_dump(mode="python"), **update})

    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json", exclude_unset=True), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


class ProfileRecord(BaseModel):
    """Contents of profiles/profile_v{N}.json."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    created_at: UtcDatetime
    cv_sha256: str
    cv_filename: str
    overrides_sha256: str
    ai_provider: str
    ai_model: str
    extracted: Profile
    profile: Profile


# --------------------------------------------------------------------------- jobs


class RawJob(BaseModel):
    """A posting as fetched from a source, before normalisation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    source_job_id: str
    is_ats: bool = Field(description="True for employer ATS boards (Greenhouse, Lever, Ashby).")
    title: str
    company: str
    locations: list[str] = Field(default_factory=list, description="Location strings as given.")
    country_hints: list[str] = Field(
        default_factory=list, description="Country codes or names supplied as separate fields."
    )
    remote_hint: str | None = None
    employment_type_hint: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    posting_url: str
    apply_url: str | None = None
    published_at: UtcDatetime | None = None
    deadline_at: UtcDatetime | None = None
    tags: list[str] = Field(default_factory=list)


class Opportunity(BaseModel):
    """One job opportunity as stored. Column names are identical in every backend."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    opportunity_id: str
    fingerprint: str
    canonical_url: str
    source: str
    source_job_id: str
    title: str
    company: str
    location_city: str | None = None
    location_country: str | None = None
    remote_type: RemoteType = RemoteType.UNKNOWN
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    required_skills: list[str] = Field(default_factory=list)
    summary: str | None = None
    posting_url: str
    apply_url: str | None = None
    apply_url_final: str | None = None
    published_at: UtcDatetime | None = None
    deadline_at: UtcDatetime | None = None
    discovered_at: UtcDatetime
    last_seen_at: UtcDatetime
    listing_status: ListingStatus = ListingStatus.ACTIVE
    relevance_score: int | None = Field(default=None, ge=0, le=100)
    prefilter_score: int | None = Field(default=None, ge=0, le=100)
    decision: Decision = Decision.PENDING_AI
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    score_reason: str | None = None
    red_flags: list[str] = Field(default_factory=list)
    profile_version: int | None = None
    ai_model: str | None = None
    is_possible_duplicate: bool = False
    duplicate_of: str | None = None
    # User-owned: written by the user, never overwritten by jobfinder.
    application_status: str | None = None
    notes: str | None = None


USER_OWNED_FIELDS: tuple[str, ...] = ("application_status", "notes")
OPPORTUNITY_COLUMNS: tuple[str, ...] = tuple(Opportunity.model_fields)


class MatchRule(StrEnum):
    NEW = "new"
    SOURCE_KEY = "source_key"
    CANONICAL_URL = "canonical_url"
    FINGERPRINT = "fingerprint"
    FUZZY = "fuzzy"


class Sighting(BaseModel):
    """A source listing that was matched to an existing opportunity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    opportunity_id: str
    source: str
    source_job_id: str
    posting_url: str
    canonical_url: str
    seen_at: UtcDatetime
    match_rule: MatchRule


SIGHTING_COLUMNS: tuple[str, ...] = tuple(Sighting.model_fields)


class UserFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application_status: str | None = None
    notes: str | None = None


# --------------------------------------------------------------------------- scoring

STRONG_MIN_SCORE = 75
POSSIBLE_MIN_SCORE = 50


def decision_for(score: int) -> Decision:
    """Decision bands; the model's own decision is replaced by this for consistency."""
    if score >= STRONG_MIN_SCORE:
        return Decision.STRONG
    if score >= POSSIBLE_MIN_SCORE:
        return Decision.POSSIBLE
    return Decision.REJECT


class ScoreResult(_AIModel):
    """The model's assessment of one posting against the profile."""

    relevance_score: int = Field(ge=0, le=100, description="Weighted rubric total, 0-100.")
    decision: Literal["strong", "possible", "reject"]
    employment_type: EmploymentType | None
    seniority: Seniority | None = Field(description="Level the posting really targets.")
    remote_type: RemoteType
    matched_skills: list[str] = Field(description="Genuine requirements the candidate meets.")
    missing_skills: list[str] = Field(description="Genuine requirements the candidate lacks.")
    required_skills: list[str] = Field(description="The posting's genuine requirements.")
    deadline: str | None = Field(description="Application deadline YYYY-MM-DD, or null.")
    red_flags: list[str] = Field(
        description="Misleading matches, keyword stuffing, eligibility or legitimacy concerns."
    )
    summary: str = Field(max_length=300, description="What the job is, at most 300 characters.")
    score_reason: str = Field(max_length=400, description="Why this score, at most 2 sentences.")

    @field_validator("deadline")
    @classmethod
    def _iso_date(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            raise ValueError("deadline must be a YYYY-MM-DD date or null") from None

    def deadline_at(self) -> datetime | None:
        """The deadline as the end of that day, UTC."""
        if self.deadline is None:
            return None
        day = date.fromisoformat(self.deadline)
        return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def profile_rows(record: ProfileRecord) -> list[tuple[str, str]]:
    """The profile as (field, value) rows for the Profile sheet/table."""
    p = record.profile
    rows = [
        ("profile_version", str(record.version)),
        ("created_at", record.created_at.strftime("%Y-%m-%d %H:%M UTC")),
        ("cv_filename", record.cv_filename),
        ("ai_model", f"{record.ai_provider}/{record.ai_model}"),
        ("seniority", p.seniority.value),
        ("years_experience", f"{p.years_experience:g}"),
        ("target_titles", "; ".join(p.target_titles)),
        ("alternative_titles", "; ".join(p.alternative_titles)),
        ("skills", "; ".join(f"{s.name} ({s.category})" for s in p.skills)),
        ("tools", "; ".join(p.tools)),
        ("programming_languages", "; ".join(p.programming_languages)),
        (
            "spoken_languages",
            "; ".join(
                f"{s.language} ({s.level})" if s.level else s.language for s in p.spoken_languages
            ),
        ),
        (
            "education",
            "; ".join(
                f"{e.degree}, {e.institution or ''} {e.end_year or ''}".strip() for e in p.education
            ),
        ),
        ("certifications", "; ".join(p.certifications)),
        ("include_keywords", "; ".join(p.include_keywords)),
        ("exclude_keywords", "; ".join(p.exclude_keywords)),
        ("synonym_groups", " | ".join(", ".join(g) for g in p.synonym_groups)),
    ]
    return rows
