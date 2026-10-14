"""Cheap, deterministic screening before any AI call.

Hard filters reject a job outright: unwanted employment type, ineligible country or
work mode, an exclude keyword in the title, a title or experience requirement far above
the candidate's level, or a posting older than ``max_age_days``.

The keyword score (0-100) measures overlap between the profile and the posting. Each
profile concept (a skill, tool, language or include keyword, together with its synonyms)
earns 3 points when it appears in the title and 1 point when it appears only in the
description; similarity between the job title and the profile's target or alternative
titles (and their synonyms, in any language listed) adds up to TITLE_FIT_POINTS, so a
posting titled like one of your target titles passes on its own. ``SATURATION`` points
or more score 100.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from jobfinder.config import RemotePreference, Settings
from jobfinder.models import Profile, RemoteType, Seniority
from jobfinder.pipeline.countries import fold
from jobfinder.pipeline.normalise import NormalisedJob, norm_title

SATURATION = 15.0
TITLE_FIT_POINTS = 9.0  # 60/100; an exact alternative title (6 points) scores 40
TITLE_WEIGHT = 3.0
DESCRIPTION_WEIGHT = 1.0
SENIORITY_GAP = 2  # e.g. a junior profile filters out senior and lead titles
YEARS_TOLERANCE = 3.0  # required years may exceed the candidate's by this much

_SENIORITY_RANK = {
    Seniority.INTERN: 0,
    Seniority.JUNIOR: 1,
    Seniority.MID: 2,
    Seniority.SENIOR: 3,
    Seniority.LEAD: 4,
}
_YEARS = re.compile(
    r"(?<![\d.])(\d{1,2})\s*\+?\s*(?:-|to|a|bis)?\s*(?:\d{1,2}\s*\+?\s*)?"
    r"(?:years?|yrs?|ans?|annees?|jahre?n?)\b[^.\n]{0,60}?"
    r"(?:experience|expertise|erfahrung|berufserfahrung)",
)


def _pattern(term: str) -> re.Pattern[str]:
    """Whole-term match that also works for 'C++', 'C#', '.NET', 'Node.js'."""
    return re.compile(rf"(?<![\w+#.]){re.escape(term)}(?![\w+#])")


@dataclass(frozen=True)
class Concept:
    name: str
    folded: tuple[re.Pattern[str], ...]
    exact: tuple[re.Pattern[str], ...]  # short terms such as "Go" or "R", matched case-sensitively

    def found_in(self, original: str, folded: str) -> bool:
        return any(p.search(folded) for p in self.folded) or any(
            p.search(original) for p in self.exact
        )


def _concept(name: str, variants: set[str]) -> Concept:
    longer = sorted({fold(v) for v in variants if len(v.strip()) > 2})
    short = sorted({v.strip() for v in variants if 0 < len(v.strip()) <= 2})
    return Concept(
        name=name,
        folded=tuple(_pattern(v) for v in longer),
        exact=tuple(_pattern(v) for v in short),
    )


@dataclass(frozen=True)
class PrefilterResult:
    passed: bool
    score: int
    reasons: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()


class Prefilter:
    def __init__(self, profile: Profile, settings: Settings) -> None:
        self.profile = profile
        self.settings = settings
        groups = [{fold(t) for t in group} for group in profile.synonym_groups]

        def synonyms(term: str) -> set[str]:
            folded = fold(term)
            variants = {term}
            for group, original in zip(groups, profile.synonym_groups, strict=True):
                if folded in group:
                    variants.update(original)
            return variants

        names = [s.name for s in profile.skills if s.category != "soft"]
        names += profile.tools + profile.programming_languages + profile.include_keywords
        seen: set[str] = set()
        concepts = []
        for name in names:
            key = fold(name)
            if not key or key in seen:
                continue
            variants = synonyms(name)
            seen.update(fold(v) for v in variants)
            concepts.append(_concept(name, variants))
        self.concepts = tuple(concepts)
        self.target_titles = tuple(
            {norm_title(v) for t in profile.target_titles for v in synonyms(t)} - {""}
        )
        self.alternative_titles = tuple(
            {norm_title(v) for t in profile.alternative_titles for v in synonyms(t)} - {""}
        )
        self.exclude = tuple(_pattern(fold(k)) for k in profile.exclude_keywords if k.strip())

    # ------------------------------------------------------------------ hard filters

    def _eligibility(self, job: NormalisedJob) -> str | None:
        return location_reason(job, self.settings)

    def _too_senior(self, job: NormalisedJob) -> str | None:
        mine = _SENIORITY_RANK[self.profile.seniority]
        if job.seniority is not None and _SENIORITY_RANK[job.seniority] - mine >= SENIORITY_GAP:
            return f"{job.seniority.value} title for a {self.profile.seniority.value} profile"
        required = required_years(job.description)
        if required is not None and required > self.profile.years_experience + YEARS_TOLERANCE:
            return f"asks for {required:g}+ years of experience"
        return None

    def hard_filter(self, job: NormalisedJob, now: datetime) -> list[str]:
        reasons = []
        if (
            job.employment_type is not None
            and job.employment_type not in self.settings.employment_types
        ):
            reasons.append(f"{job.employment_type.value} not wanted")
        if job.published_at is not None:
            age = now - job.published_at
            if age > timedelta(days=self.settings.max_age_days):
                reasons.append(f"posted {age.days} days ago")
        if (reason := self._eligibility(job)) is not None:
            reasons.append(reason)
        folded_title = fold(job.title)
        if any(p.search(folded_title) for p in self.exclude):
            reasons.append("exclude keyword in title")
        if (reason := self._too_senior(job)) is not None:
            reasons.append(reason)
        return reasons

    # ------------------------------------------------------------------ score

    def _title_points(self, job_title: str) -> float:
        def best(titles: tuple[str, ...]) -> float:
            return max(
                (
                    (fuzz.token_set_ratio(job_title, t) + fuzz.token_sort_ratio(job_title, t)) / 2
                    for t in titles
                ),
                default=0.0,
            )

        target, alternative = best(self.target_titles), best(self.alternative_titles)
        if target >= 90:
            return TITLE_FIT_POINTS  # one of your target titles: enough to pass on its own
        if target >= 75 or alternative >= 90:
            return TITLE_FIT_POINTS * 2 / 3
        if alternative >= 75:
            return TITLE_FIT_POINTS / 3
        return 0.0

    def keyword_score(self, job: NormalisedJob) -> tuple[int, tuple[str, ...]]:
        title_folded = fold(job.title)
        description_folded = fold(job.description)
        points = self._title_points(norm_title(job.title))
        matched = []
        for concept in self.concepts:
            if concept.found_in(job.title, title_folded):
                points += TITLE_WEIGHT
                matched.append(concept.name)
            elif concept.found_in(job.description, description_folded):
                points += DESCRIPTION_WEIGHT
                matched.append(concept.name)
        return round(min(1.0, points / SATURATION) * 100), tuple(matched)

    def evaluate(self, job: NormalisedJob, now: datetime) -> PrefilterResult:
        score, matched = self.keyword_score(job)
        reasons = self.hard_filter(job, now)
        if not reasons and score < self.settings.prefilter_threshold:
            reasons.append(f"keyword score {score} below {self.settings.prefilter_threshold}")
        return PrefilterResult(
            passed=not reasons, score=score, reasons=tuple(reasons), matched=matched
        )


def location_reason(job: NormalisedJob, settings: Settings) -> str | None:
    """Why the job's place or work mode rules it out for you, or None if it may fit.

    Jobs with an unknown location, and remote jobs open to a region (EMEA, worldwide),
    are given the benefit of the doubt.
    """
    targets = set(settings.target_countries)
    preference = settings.remote_preference
    remote = job.remote_type is RemoteType.REMOTE
    if preference is RemotePreference.REMOTE_ONLY and job.remote_type in (
        RemoteType.ONSITE,
        RemoteType.HYBRID,
    ):
        return f"{job.remote_type.value} role, remote only wanted"
    if preference is RemotePreference.ONSITE_ONLY and remote:
        return "remote role, on-site only wanted"
    if remote:
        if job.countries and not job.countries & targets:
            return f"remote but limited to {', '.join(sorted(job.countries))}"
        return None
    countries = set(job.countries) | ({job.location_country} if job.location_country else set())
    if countries and not countries & targets:
        return f"located in {', '.join(sorted(countries))}"
    return None


def required_years(description: str) -> float | None:
    """Largest 'N years of experience' requirement mentioned, if any (capped at 20)."""
    values = [int(m.group(1)) for m in _YEARS.finditer(fold(description))]
    values = [v for v in values if 0 < v <= 20]
    return float(max(values)) if values else None
