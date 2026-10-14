from __future__ import annotations

import pytest

from jobfinder.models import EmploymentType, RawJob, RemoteType, Seniority
from jobfinder.pipeline.countries import country_code
from jobfinder.pipeline.normalise import (
    canonical_url,
    employment_type_of,
    fingerprint,
    norm_company,
    norm_title,
    normalise,
    parse_locations,
    parse_place,
    seniority_of,
)
from jobfinder.pipeline.text import html_to_text

# --------------------------------------------------------------------------- URLs


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Jobs.Lever.CO/acme/123/", "https://jobs.lever.co/acme/123"),
        ("https://x.io/job?utm_source=li&utm_medium=x&id=5", "https://x.io/job?id=5"),
        ("https://x.io/job?gh_src=abc&lever-source=LinkedIn", "https://x.io/job"),
        ("https://x.io/job?ref=hn&fbclid=1&gclid=2", "https://x.io/job"),
        ("https://x.io/job?b=2&a=1", "https://x.io/job?a=1&b=2"),
        ("https://x.io/job#apply", "https://x.io/job"),
        ("https://x.io:443/job", "https://x.io/job"),
        ("http://x.io:8080/job", "http://x.io:8080/job"),
        ("https://x.io/", "https://x.io"),
        ("https://x.io/Job/ABC", "https://x.io/Job/ABC"),  # path case is significant
    ],
)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_canonical_url_keeps_meaningful_params() -> None:
    assert canonical_url("https://x.io/j?gh_jid=42&utm_x=1") == "https://x.io/j?gh_jid=42"


# --------------------------------------------------------------------------- keys


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer (m/w/d)",
        "Software Engineer (w/m/d)",
        "Software Engineer (H/F)",
        "Software Engineer (f/h)",
        "Software Engineer (m/f/x)",
        "Software Engineer (all genders)",
        "Software Engineer m/w/d",
        "Software Engineer - m/f/d",
        "Software-Engineer",
        "Software Engineer!",
        "SOFTWARE ENGINEER",
    ],
)
def test_norm_title_equivalents(title: str) -> None:
    assert norm_title(title) == "software engineer"


def test_norm_title_strips_accents_and_entities() -> None:
    assert norm_title("Ingénieur Données &amp; IA (H/F)") == "ingenieur donnees ia"


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Acme Inc.", "acme"),
        ("Acme, Inc", "acme"),
        ("ACME LLC", "acme"),
        ("Acme GmbH", "acme"),
        ("Acme SARL", "acme"),
        ("Acme SAS", "acme"),
        ("Acme S.A.", "acme"),
        ("Acme Ltd", "acme"),
        ("Société Générale SA", "societe generale"),
        ("SAS Institute", "sas institute"),  # leading token is not a suffix
        ("Inc", "inc"),  # a name is never stripped to nothing
    ],
)
def test_norm_company(company: str, expected: str) -> None:
    assert norm_company(company) == expected


def test_fingerprint_equal_for_variants() -> None:
    assert fingerprint("Acme GmbH", "Backend Engineer (m/w/d)", "München") == fingerprint(
        "ACME", "Backend-Engineer", "Munchen"
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("Acme", "Backend Engineer", "Berlin"), ("Acme", "Backend Engineer", "Paris")),
        (("Acme", "Backend Engineer", "Berlin"), ("Globex", "Backend Engineer", "Berlin")),
        (("Acme", "Backend Engineer", "Berlin"), ("Acme", "Frontend Engineer", "Berlin")),
        (("Acme", "Backend Engineer", None), ("Acme", "Backend Engineer", "Berlin")),
    ],
)
def test_fingerprint_differs(
    a: tuple[str, str, str | None], b: tuple[str, str, str | None]
) -> None:
    assert fingerprint(*a) != fingerprint(*b)


def test_fingerprint_remote_literal() -> None:
    assert fingerprint("Acme", "Dev", None) == fingerprint("Acme", "Dev", "Remote")


# --------------------------------------------------------------------------- locations


@pytest.mark.parametrize(
    ("text", "city", "country", "remote"),
    [
        ("Remote, France", None, "FR", True),
        ("Remote Ireland", None, "IE", True),
        ("Remote (Canada)", None, "CA", True),
        ("Remote, US", None, "US", True),
        ("Remote, EMEA", None, None, True),
        ("Worldwide", None, None, True),
        ("Bangalore, India", "Bangalore", "IN", False),
        ("Singapore, Singapore", "Singapore", "SG", False),
        ("New York, NY (HQ)", "New York", "US", False),
        ("San Francisco, CA", "San Francisco", "US", False),
        ("Toronto, ON", "Toronto", "CA", False),
        ("Berlin, DE", "Berlin", "DE", False),
        ("Washington, D.C.", "Washington", None, False),
        ("Abu Dhabi, United Arab Emirates", "Abu Dhabi", "AE", False),
        ("Tunis, Tunisie", "Tunis", "TN", False),
        ("München", "München", "DE", False),
        ("Paris", "Paris", "FR", False),
        ("Hybrid - London", "London", "GB", False),
    ],
)
def test_parse_place(text: str, city: str | None, country: str | None, remote: bool) -> None:
    place = parse_place(text)
    assert (place.city, place.country, place.remote) == (city, country, remote)


def test_parse_locations_prefers_target_country() -> None:
    info = parse_locations(
        ["Remote, EMEA; Remote, France; Remote, Germany; Remote, United Kingdom"], [], ["DE", "TN"]
    )
    assert info.country == "DE"
    assert info.countries == {"FR", "DE", "GB"}
    assert info.remote


def test_parse_locations_falls_back_to_first_country_and_hints() -> None:
    assert parse_locations(["Bangalore, India", "Paris"], [], ["TN"]).country == "IN"
    assert parse_locations(["Somewhere"], ["USA", "Canada"], ["CA"]).country == "CA"
    assert parse_locations([], [], []).country is None


@pytest.mark.parametrize(
    ("text", "code"),
    [("USA", "US"), ("UK", "GB"), ("Deutschland", "DE"), ("Côte d'Ivoire", "CI"), ("XX", None)],
)
def test_country_code(text: str, code: str | None) -> None:
    assert country_code(text) == code


# --------------------------------------------------------------------------- job type


@pytest.mark.parametrize(
    ("hint", "title", "expected"),
    [
        ("Full-time", "Engineer", EmploymentType.FULL_TIME),
        ("FullTime", "Engineer", EmploymentType.FULL_TIME),
        ("full_time", "Engineer", EmploymentType.FULL_TIME),
        ("Fixed-Term", "Engineer", EmploymentType.TEMPORARY),
        ("Intern", "Scientist", EmploymentType.INTERNSHIP),
        ("freelance", "Designer", EmploymentType.CONTRACT),
        ("CDI", "Développeur", EmploymentType.FULL_TIME),
        (None, "Deployment Strategist, Internship", EmploymentType.INTERNSHIP),
        ("Full-time", "Stagiaire Data (H/F)", EmploymentType.INTERNSHIP),
        (None, "Werkstudent Software (m/w/d)", EmploymentType.PART_TIME),
        (None, "Alternance - Développeur", EmploymentType.APPRENTICESHIP),
        (None, "Internal Tools Engineer", None),  # "internal" is not "intern"
        ("other", "Engineer", None),
    ],
)
def test_employment_type(hint: str | None, title: str, expected: EmploymentType | None) -> None:
    assert employment_type_of(hint, title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Backend Engineer", Seniority.SENIOR),
        ("Sr. Data Engineer", Seniority.SENIOR),
        ("Staff Engineer", Seniority.LEAD),
        ("Engineering Lead", Seniority.LEAD),
        ("Head of Data", Seniority.LEAD),
        ("Junior Developer", Seniority.JUNIOR),
        ("Graduate Software Engineer", Seniority.JUNIOR),
        ("Software Engineer Intern", Seniority.INTERN),
        ("Software Engineer", None),
        ("Leadership Coach", None),  # "lead" must be a whole word
    ],
)
def test_seniority(title: str, expected: Seniority | None) -> None:
    assert seniority_of(title) == expected


# --------------------------------------------------------------------------- text and job


def test_html_to_text_handles_double_escaping() -> None:
    markup = (
        "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    )
    assert html_to_text(markup) == "Build & ship\n\n- Python"


def test_html_to_text_drops_scripts() -> None:
    assert html_to_text("<p>a</p><script>alert(1)</script><style>p{}</style>b") == "a\nb"


def _raw(**kw: object) -> RawJob:
    data: dict[str, object] = {
        "source": "lever",
        "source_job_id": "1",
        "is_ats": True,
        "title": "Backend Engineer (m/w/d)",
        "company": "Acme GmbH",
        "posting_url": "https://jobs.lever.co/acme/1?lever-source=x",
    }
    data.update(kw)
    return RawJob.model_validate(data)


def test_normalise_remote_job_has_no_city_in_fingerprint() -> None:
    job = normalise(_raw(locations=["Berlin"], remote_hint="remote"), ["DE"])
    assert job.remote_type is RemoteType.REMOTE
    assert job.location_city == "Berlin"
    assert job.fingerprint == fingerprint("Acme", "Backend Engineer", None)
    assert job.canonical_url == "https://jobs.lever.co/acme/1"


def test_normalise_onsite_from_city() -> None:
    job = normalise(_raw(locations=["Berlin"]), ["DE"])
    assert job.remote_type is RemoteType.ONSITE
    assert job.location_country == "DE"
    assert job.fingerprint == fingerprint("Acme", "Backend Engineer", "Berlin")


def test_normalise_description_from_html() -> None:
    job = normalise(_raw(description_html="&lt;p&gt;Hello&lt;/p&gt;"), [])
    assert job.description == "Hello"


def test_country_lists_are_expanded() -> None:
    info = parse_locations(["Estonia,  Spain"], [], ["ES"])
    assert info.countries == {"EE", "ES"}
    assert info.country == "ES"
    info = parse_locations(["USA, UK, India, Europe"], [], [])
    assert info.countries == {"US", "GB", "IN"}
    # a city and its country is not a list
    assert parse_locations(["Paris, France"], [], []).city == "Paris"
    assert parse_locations(["Singapore, Singapore"], [], []).city == "Singapore"
