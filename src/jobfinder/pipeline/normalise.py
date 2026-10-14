"""Normalisation of fetched postings: canonical URLs, dedupe keys, location, job type."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobfinder.models import (
    EmploymentType,
    Opportunity,
    RawJob,
    RemoteType,
    Seniority,
)
from jobfinder.pipeline.countries import (
    CA_PROVINCES,
    US_STATES,
    city_country,
    country_code,
    fold,
    is_region,
)
from jobfinder.pipeline.text import html_to_text, unescape_fully
from jobfinder.ulid import new_ulid

# --------------------------------------------------------------------------- URLs

_TRACKING_PARAMS = frozenset({"gh_src", "lever-source", "ref", "fbclid", "gclid"})


def canonical_url(url: str) -> str:
    """Lowercase scheme/host, drop default port, fragment, trailing slash and tracking
    parameters (utm_*, gh_src, lever-source, ref, fbclid, gclid); sort the rest."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((scheme, host, path, urlencode(query), ""))


# --------------------------------------------------------------------------- dedupe keys

_GENDER_PAREN = re.compile(
    r"\(\s*(?:[mwfhdx]\s*(?:[/|,*]\s*[mwfhdx]\s*){1,3}|all genders?|gn|m/w/divers|tous genres)"
    r"\s*\)",
    re.IGNORECASE,
)
_GENDER_BARE = re.compile(
    r"(?:^|\s)[-\u2013]?\s*[mwfhdx](?:\s*/\s*[mwfhdx]){1,3}(?=\s|$)", re.IGNORECASE
)
_NON_WORD = re.compile(r"[^\w\s]|_")
_SPACES = re.compile(r"\s+")
LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "ltd", "limited", "llc", "llp", "gmbh", "ag", "kg", "ug",
        "sarl", "sas", "sa", "plc", "corp", "corporation", "co", "bv", "nv", "srl", "spa",
        "ab", "oy", "se",
    }
)  # fmt: skip


def _clean(text: str) -> str:
    return _SPACES.sub(" ", _NON_WORD.sub(" ", fold(text))).strip()


def strip_gender_markers(title: str) -> str:
    return _GENDER_BARE.sub(" ", _GENDER_PAREN.sub(" ", title))


def norm_title(title: str) -> str:
    return _clean(strip_gender_markers(unescape_fully(title)))


def norm_company(company: str) -> str:
    # Dots are dropped first so that "S.A." and "Inc." match the suffix list.
    tokens = _clean(unescape_fully(company).replace(".", "")).split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def norm_city(city: str | None) -> str:
    return _clean(city) if city else "remote"


def fingerprint(company: str, title: str, city: str | None) -> str:
    key = f"{norm_company(company)}|{norm_title(title)}|{norm_city(city)}"
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()


# --------------------------------------------------------------------------- location

_REMOTE_WORDS = re.compile(
    r"\b(?:fully remote|remote|anywhere|worldwide|teletravail|home ?office|work from home|wfh)\b"
)
_HYBRID_WORDS = re.compile(r"\b(?:hybrid|hybride)\b")
_ONSITE_WORDS = re.compile(r"\b(?:on-?site|in office|presentiel)\b")
_HQ = re.compile(r"\((?:hq|headquarters)\)", re.IGNORECASE)
_LOCATION_SPLIT = re.compile(r";|\||\bor\b")
_TOKEN_SPLIT = re.compile(r",|/|\s[-\u2013]\s|\(|\)")
_WORK_MODE_WORDS = re.compile(
    r"\b(?:fully remote|remote|hybrid|hybride|on-?site|in office|teletravail|home ?office|"
    r"work from home|wfh|only|first|friendly|based)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlaceInfo:
    city: str | None = None
    country: str | None = None
    remote: bool = False
    hybrid: bool = False
    onsite: bool = False


def parse_place(text: str) -> PlaceInfo:
    """Parse one location such as 'Remote, France', 'New York, NY (HQ)', 'Berlin'."""
    folded = fold(text)
    remote = bool(_REMOTE_WORDS.search(folded))
    hybrid = bool(_HYBRID_WORDS.search(folded))
    onsite = bool(_ONSITE_WORDS.search(folded))
    without_mode = _WORK_MODE_WORDS.sub(" ", _HQ.sub(" ", text))
    tokens = [t.strip(" .-\u2013") for t in _TOKEN_SPLIT.split(without_mode)]
    tokens = [t for t in tokens if t]

    city: str | None = None
    country: str | None = None
    state_country: str | None = None
    for token in tokens:
        if is_region(token):
            continue
        if len(token) == 2 and token.isupper() and city is not None:
            # "Berlin, DE", "San Francisco, CA", "Toronto, ON": a known city decides;
            # otherwise a US state or Canadian province code beats a country code.
            if city_country(city) is not None:
                continue
            if token in US_STATES:
                state_country = state_country or "US"
                continue
            if token in CA_PROVINCES:
                state_country = state_country or "CA"
                continue
        code = country_code(token)
        if code is not None:
            country = country or code
            if city is None and city_country(token) is not None:  # city-states
                city = token
        elif city is None:
            city = token
    if city is not None:
        country = country or city_country(city) or state_country
    return PlaceInfo(city=city, country=country, remote=remote, hybrid=hybrid, onsite=onsite)


def _expand_country_list(text: str) -> list[PlaceInfo]:
    """'Estonia, Spain' or 'USA, UK, India' lists countries, not a city and a country.

    "Singapore, Singapore" is a city in a country, so tokens that are also known cities
    never make a list.
    """
    tokens = [t.strip(" .") for t in _WORK_MODE_WORDS.sub(" ", text).split(",")]
    tokens = [t for t in tokens if t]
    codes = [country_code(t) for t in tokens]
    is_list = len(tokens) > 1 and all(
        (code is not None and city_country(token) is None) or is_region(token)
        for code, token in zip(codes, tokens, strict=True)
    )
    if not is_list:
        return [parse_place(text)]
    mode = parse_place(text)
    countries = [code for code in codes if code is not None]
    return [
        PlaceInfo(country=code, remote=mode.remote, hybrid=mode.hybrid, onsite=mode.onsite)
        for code in countries
    ] or [mode]


@dataclass(frozen=True)
class LocationInfo:
    city: str | None
    country: str | None
    countries: frozenset[str]
    remote: bool
    hybrid: bool
    onsite: bool


def parse_locations(
    locations: list[str], country_hints: list[str], preferred_countries: list[str]
) -> LocationInfo:
    """Combine location strings into one primary place, preferring target countries."""
    places = [
        place
        for location in locations
        for part in _LOCATION_SPLIT.split(location)
        if part.strip()
        for place in _expand_country_list(part)
    ]
    hinted = [code for code in (country_code(h) for h in country_hints) if code]
    countries = {p.country for p in places if p.country} | set(hinted)
    preferred = set(preferred_countries)
    primary = (
        next((p for p in places if p.country in preferred), None)
        or next((p for p in places if p.country), None)
        or (places[0] if places else PlaceInfo())
    )
    country = primary.country
    if country is None and hinted:
        country = next((c for c in hinted if c in preferred), hinted[0])
    return LocationInfo(
        city=primary.city,
        country=country,
        countries=frozenset(countries),
        remote=any(p.remote for p in places),
        hybrid=any(p.hybrid for p in places),
        onsite=any(p.onsite for p in places),
    )


def remote_type_of(hint: str | None, location: LocationInfo, title: str) -> RemoteType:
    if hint:
        key = fold(hint).replace("-", "").replace("_", "").replace(" ", "")
        mapped = {
            "remote": RemoteType.REMOTE,
            "hybrid": RemoteType.HYBRID,
            "onsite": RemoteType.ONSITE,
            "inoffice": RemoteType.ONSITE,
        }.get(key)
        if mapped is not None:
            return mapped
    folded_title = fold(title)
    if location.hybrid or _HYBRID_WORDS.search(folded_title):
        return RemoteType.HYBRID
    if location.remote or _REMOTE_WORDS.search(folded_title):
        return RemoteType.REMOTE
    if location.onsite or location.city:
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


# --------------------------------------------------------------------------- job type

_EMPLOYMENT_HINTS: dict[str, EmploymentType] = {
    "fulltime": EmploymentType.FULL_TIME, "permanent": EmploymentType.FULL_TIME,
    "cdi": EmploymentType.FULL_TIME, "regular": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME, "teilzeit": EmploymentType.PART_TIME,
    "vollzeit": EmploymentType.FULL_TIME,
    "contract": EmploymentType.CONTRACT, "contractor": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT, "cdd": EmploymentType.CONTRACT,
    "fixedterm": EmploymentType.TEMPORARY, "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY, "seasonal": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP, "internship": EmploymentType.INTERNSHIP,
    "stage": EmploymentType.INTERNSHIP, "praktikum": EmploymentType.INTERNSHIP,
    "apprenticeship": EmploymentType.APPRENTICESHIP, "apprentice": EmploymentType.APPRENTICESHIP,
    "alternance": EmploymentType.APPRENTICESHIP, "ausbildung": EmploymentType.APPRENTICESHIP,
    "werkstudent": EmploymentType.PART_TIME,
    "unbefristeterarbeitsvertrag": EmploymentType.FULL_TIME,
    "befristeterarbeitsvertrag": EmploymentType.TEMPORARY,
    "minijob": EmploymentType.PART_TIME, "interim": EmploymentType.TEMPORARY,
    "mis": EmploymentType.TEMPORARY, "sai": EmploymentType.TEMPORARY,
    "lib": EmploymentType.CONTRACT, "freelancer": EmploymentType.CONTRACT,
    "contratapprentissage": EmploymentType.APPRENTICESHIP,
    "contratdeprofessionnalisation": EmploymentType.APPRENTICESHIP,
}  # fmt: skip
_TITLE_INTERN = re.compile(r"\b(?:intern|internship|stagiaire|stage|praktikant\w*|praktikum)\b")
_TITLE_APPRENTICE = re.compile(r"\b(?:apprenti\w*|alternan\w*|ausbildung|azubi)\b")
_TITLE_WORKING_STUDENT = re.compile(r"\b(?:werkstudent\w*|working student)\b")


def employment_type_of(hint: str | None, title: str) -> EmploymentType | None:
    folded_title = fold(title)
    if _TITLE_INTERN.search(folded_title):
        return EmploymentType.INTERNSHIP
    if _TITLE_APPRENTICE.search(folded_title):
        return EmploymentType.APPRENTICESHIP
    if _TITLE_WORKING_STUDENT.search(folded_title):
        return EmploymentType.PART_TIME
    if hint:
        key = re.sub(r"[\s_-]", "", fold(hint))
        if key in _EMPLOYMENT_HINTS:
            return _EMPLOYMENT_HINTS[key]
    return None


_SENIORITY_RULES: list[tuple[re.Pattern[str], Seniority]] = [
    (_TITLE_INTERN, Seniority.INTERN),
    (re.compile(r"\b(?:head of|director|vp|vice president|principal|staff|lead|chief)\b"),
     Seniority.LEAD),
    (re.compile(r"\b(?:senior|sr|snr)\b"), Seniority.SENIOR),
    (re.compile(r"\b(?:junior|jr|entry level|entry-level|graduate|new grad|debutant)\b"),
     Seniority.JUNIOR),
    (re.compile(r"\b(?:mid level|mid-level|intermediate|confirme)\b"), Seniority.MID),
]  # fmt: skip


def seniority_of(title: str) -> Seniority | None:
    folded = fold(title)
    for pattern, level in _SENIORITY_RULES:
        if pattern.search(folded):
            return level
    return None


# --------------------------------------------------------------------------- job


@dataclass(frozen=True)
class NormalisedJob:
    source: str
    source_job_id: str
    is_ats: bool
    title: str
    company: str
    company_norm: str
    title_norm: str
    fingerprint: str
    canonical_url: str
    posting_url: str
    apply_url: str | None
    location_city: str | None
    location_country: str | None
    countries: frozenset[str]
    remote_type: RemoteType
    employment_type: EmploymentType | None
    seniority: Seniority | None
    published_at: datetime | None
    deadline_at: datetime | None
    description: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_opportunity(self, now: datetime) -> Opportunity:
        return Opportunity(
            opportunity_id=new_ulid(now),
            fingerprint=self.fingerprint,
            canonical_url=self.canonical_url,
            source=self.source,
            source_job_id=self.source_job_id,
            title=self.title,
            company=self.company,
            location_city=self.location_city,
            location_country=self.location_country,
            remote_type=self.remote_type,
            employment_type=self.employment_type,
            seniority=self.seniority,
            posting_url=self.posting_url,
            apply_url=self.apply_url,
            published_at=self.published_at,
            deadline_at=self.deadline_at,
            discovered_at=now,
            last_seen_at=now,
        )


_TAG = re.compile(r"<[^>]{1,200}>")


def _tidy(text: str) -> str:
    """Unescape entities, drop inline HTML (e.g. Adzuna's <strong> highlights), squeeze spaces."""
    return _SPACES.sub(" ", _TAG.sub("", unescape_fully(text))).strip()


def normalise(raw: RawJob, preferred_countries: list[str]) -> NormalisedJob:
    title = _tidy(raw.title)
    company = _tidy(raw.company)
    location = parse_locations(raw.locations, raw.country_hints, preferred_countries)
    remote_type = remote_type_of(raw.remote_hint, location, title)
    city = location.city if remote_type is not RemoteType.REMOTE else None
    if raw.description_text:
        description = raw.description_text.strip()
    elif raw.description_html:
        description = html_to_text(raw.description_html)
    else:
        description = ""
    return NormalisedJob(
        source=raw.source,
        source_job_id=raw.source_job_id,
        is_ats=raw.is_ats,
        title=title,
        company=company,
        company_norm=norm_company(company),
        title_norm=norm_title(title),
        fingerprint=fingerprint(company, title, city),
        canonical_url=canonical_url(raw.posting_url),
        posting_url=raw.posting_url,
        apply_url=raw.apply_url,
        location_city=location.city,
        location_country=location.country,
        countries=location.countries,
        remote_type=remote_type,
        employment_type=employment_type_of(raw.employment_type_hint, title),
        seniority=seniority_of(title),
        published_at=raw.published_at,
        deadline_at=raw.deadline_at,
        description=description,
        tags=tuple(raw.tags),
    )
