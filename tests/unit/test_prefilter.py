from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jobfinder.ai.examples import PROFILE_EXAMPLE
from jobfinder.config import AppConfig, RemotePreference, Settings
from jobfinder.models import EmploymentType, Profile, Seniority
from jobfinder.pipeline.normalise import normalise
from jobfinder.pipeline.prefilter import Prefilter, required_years
from tests.helpers import make_raw_job

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
# PROFILE_EXAMPLE: junior, 0.5 years; Python, SQL, Django, Docker, Git, REST API design;
# targets "Junior Backend Developer", "Python Developer"; excludes "10+ years".


@pytest.fixture
def settings(app_config: AppConfig) -> Settings:
    # target_countries TN, FR, DE; remote_first; full_time + internship; 30 days; threshold 35
    return app_config.settings


def evaluate(settings: Settings, profile: Profile = PROFILE_EXAMPLE, **job: Any):  # type: ignore[no-untyped-def]
    return Prefilter(profile, settings).evaluate(
        normalise(make_raw_job(**job), settings.target_countries), NOW
    )


def test_good_match_passes_with_high_score(settings: Settings) -> None:
    result = evaluate(settings)
    assert result.passed, result.reasons
    # title fit 9 + "Python" in title 3 + "Django" in text 1 = 13 of 15 points
    assert result.score == 87
    assert set(result.matched) == {"Python", "Django"}


@pytest.mark.parametrize(
    ("job", "reason"),
    [
        ({"employment_type_hint": "Contract"}, "contract not wanted"),
        ({"published_at": NOW - timedelta(days=45)}, "posted 45 days ago"),
        ({"locations": ["Bangalore, India"], "remote_hint": None}, "located in IN"),
        (
            {"locations": ["Remote, United States"], "remote_hint": "remote"},
            "remote but limited to US",
        ),
        ({"title": "Senior Backend Developer (Python)"}, "senior title for a junior profile"),
        ({"title": "Staff Python Engineer"}, "lead title for a junior profile"),
        (
            {"description_text": "Python and Django. 5+ years of professional experience."},
            "asks for 5+ years of experience",
        ),
        ({"title": "Python Developer, 10+ years"}, "exclude keyword in title"),
    ],
)
def test_hard_filters(settings: Settings, job: dict[str, Any], reason: str) -> None:
    result = evaluate(settings, **job)
    assert not result.passed
    assert reason in result.reasons


@pytest.mark.parametrize(
    "job",
    [
        {"locations": ["Remote, EMEA"], "remote_hint": "remote"},  # region: eligible
        {"locations": ["Worldwide"], "remote_hint": "remote"},
        {"locations": ["Remote, Canada; Remote, Germany"], "remote_hint": "remote"},
        {"locations": ["London; Berlin"], "remote_hint": "hybrid"},  # one target country
        {"employment_type_hint": None},  # unknown type is not filtered
        {"published_at": None},
        {"title": "Python Developer (Mid-level)"},  # one level above junior is fine
        {"description_text": "Python, Django and REST APIs. 3 years of experience preferred."},
    ],
)
def test_not_filtered(settings: Settings, job: dict[str, Any]) -> None:
    result = evaluate(settings, **job)
    assert result.passed, result.reasons


def test_remote_preference(settings: Settings) -> None:
    remote_only = settings.model_copy(update={"remote_preference": RemotePreference.REMOTE_ONLY})
    assert "hybrid role, remote only wanted" in evaluate(remote_only).reasons
    onsite_only = settings.model_copy(update={"remote_preference": RemotePreference.ONSITE_ONLY})
    result = evaluate(onsite_only, remote_hint="remote", locations=["Remote, France"])
    assert "remote role, on-site only wanted" in result.reasons


def test_low_keyword_score_is_filtered(settings: Settings) -> None:
    result = evaluate(
        settings,
        title="Account Executive",
        description_text="Sell our platform to enterprise customers. Salesforce experience.",
    )
    assert not result.passed
    assert result.score < 35
    assert any(r.startswith("keyword score") for r in result.reasons)


def test_title_matches_weigh_three_times(settings: Settings) -> None:
    # No target titles, so only concept matches contribute.
    profile = PROFILE_EXAMPLE.model_copy(update={"target_titles": [], "alternative_titles": []})
    in_title = evaluate(
        settings, profile, title="Docker Specialist", description_text="Containers."
    )
    in_text = evaluate(settings, profile, title="Specialist", description_text="Docker containers.")
    assert (in_title.score, in_text.score) == (20, 7)  # 3 and 1 points of 15


def test_synonyms_count_as_the_same_concept(settings: Settings) -> None:
    profile = PROFILE_EXAMPLE.model_copy(
        update={"tools": ["PostgreSQL"], "synonym_groups": [["PostgreSQL", "Postgres"]]}
    )
    result = evaluate(
        settings, profile, title="Engineer", description_text="We use Postgres daily."
    )
    assert "PostgreSQL" in result.matched


def test_short_terms_need_exact_case(settings: Settings) -> None:
    profile = PROFILE_EXAMPLE.model_copy(update={"programming_languages": ["Go"], "tools": []})
    hit = evaluate(settings, profile, title="Engineer", description_text="Services written in Go.")
    miss = evaluate(settings, profile, title="Engineer", description_text="Ready to go live.")
    assert "Go" in hit.matched
    assert "Go" not in miss.matched


def test_symbols_in_terms(settings: Settings) -> None:
    profile = PROFILE_EXAMPLE.model_copy(update={"programming_languages": ["C++", "C#"]})
    result = evaluate(settings, profile, title="Engineer", description_text="C++ and C# required.")
    assert {"C++", "C#"} <= set(result.matched)


def test_internship_for_intern_profile(settings: Settings) -> None:
    intern = PROFILE_EXAMPLE.model_copy(
        update={"seniority": Seniority.INTERN, "years_experience": 0}
    )
    result = evaluate(settings, intern, title="Backend Developer Intern (Python)")
    assert result.passed, result.reasons
    assert normalise(make_raw_job(title="Backend Developer Intern"), []).employment_type is (
        EmploymentType.INTERNSHIP
    )


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("3+ years of experience with Python", 3),
        ("2-4 years of relevant experience; 5 years of Java experience is a plus", 5),
        ("au moins 3 ans d'expérience", 3),
        ("mindestens 4 Jahre Berufserfahrung", 4),
        ("Founded 10 years ago.", None),
        ("Python 3.12 experience", None),
    ],
)
def test_required_years(text: str, years: int | None) -> None:
    assert required_years(text) == years


def test_title_in_another_language_matches_through_synonyms(settings: Settings) -> None:
    profile = PROFILE_EXAMPLE.model_copy(
        update={
            "target_titles": ["System Administrator"],
            "synonym_groups": [
                ["System Administrator", "IT-Administrator", "Administrateur systèmes"]
            ],
        }
    )
    for title in ("IT-Administrator (m/w/d)", "Administrateur systèmes H/F"):
        result = evaluate(settings, profile, title=title, description_text="Betrieb der Server.")
        assert result.passed, (title, result.reasons)
        assert result.score == 60  # the title alone carries it past the threshold


def test_exact_alternative_title_scores_40(settings: Settings) -> None:
    profile = PROFILE_EXAMPLE.model_copy(
        update={
            "target_titles": [],
            "alternative_titles": ["Infrastructure Engineer"],
            "tools": [],
            "programming_languages": [],
            "skills": [],
            "include_keywords": [],
        }
    )
    result = evaluate(settings, profile, title="Infrastructure Engineer", description_text="x")
    assert result.score == 40 and result.passed
