"""Valid example outputs, shown to JSON-mode providers next to the schema."""

from __future__ import annotations

from jobfinder.models import (
    Education,
    EmploymentType,
    Profile,
    RemoteType,
    ScoreResult,
    Seniority,
    Skill,
    SpokenLanguage,
)

PROFILE_EXAMPLE = Profile(
    skills=[
        Skill(
            name="REST API design",
            category="technical",
            evidence="Designed REST endpoints for an order-tracking service",
        ),
        Skill(name="E-commerce", category="domain", evidence="Internship at an online retailer"),
    ],
    tools=["Django", "Docker", "Git"],
    programming_languages=["Python", "SQL"],
    spoken_languages=[
        SpokenLanguage(language="Arabic", level="native"),
        SpokenLanguage(language="English", level="B2"),
    ],
    education=[
        Education(
            degree="Engineering degree",
            field_of_study="Computer Science",
            institution="Example University",
            end_year=2026,
        )
    ],
    certifications=[],
    years_experience=0.5,
    seniority=Seniority.JUNIOR,
    target_titles=["Junior Backend Developer", "Python Developer"],
    alternative_titles=["Software Engineer Intern", "Data Engineer"],
    synonym_groups=[["Backend Developer", "Back-end Developer", "Développeur back-end"]],
    include_keywords=["python", "django", "api"],
    exclude_keywords=["10+ years"],
)


SCORE_EXAMPLE = ScoreResult(
    relevance_score=68,
    decision="possible",
    employment_type=EmploymentType.FULL_TIME,
    seniority=Seniority.JUNIOR,
    remote_type=RemoteType.HYBRID,
    matched_skills=["Python", "REST APIs", "PostgreSQL"],
    missing_skills=["Kubernetes"],
    required_skills=["Python", "REST APIs", "PostgreSQL", "Kubernetes"],
    deadline=None,
    red_flags=["Lists 15 cloud tools; only Python and PostgreSQL appear in the duties"],
    summary="Junior backend developer building internal REST services in Python for a "
    "logistics company; hybrid in Tunis.",
    score_reason="Core backend skills match and the level fits; Kubernetes is missing and "
    "the tool list is inflated.",
)
