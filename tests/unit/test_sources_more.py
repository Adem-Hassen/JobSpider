"""Parsers and fetch behaviour for Remote OK, Jobicy, Himalayas, Arbeitnow, Adzuna,
France Travail and e-mail alerts."""

from __future__ import annotations

import email
import email.policy
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobfinder.http.client import (
    DailyCapReachedError,
    HttpClient,
    RequestPolicy,
    SourceAuthError,
    create_async_client,
)
from jobfinder.models import EmploymentType, RemoteType
from jobfinder.pipeline.normalise import normalise
from jobfinder.sources import adzuna, arbeitnow, france_travail, himalayas, jobicy, remoteok
from jobfinder.sources.base import FetchContext, FetchReport
from jobfinder.sources.email_alerts import PARSERS, EmailAlertsSource, parse_message
from jobfinder.storage.state_store import SourceState, StateStore
from tests.helpers import FIXTURES, FakeClock, load_source_fixture

T0 = datetime(2026, 10, 14, 12, tzinfo=UTC)


def body(name: str) -> Any:
    return load_source_fixture(name)["body"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
async def http(tmp_path: Path, clock: FakeClock) -> AsyncIterator[HttpClient]:
    async with create_async_client("me@example.com") as client:
        yield HttpClient(client, StateStore(tmp_path / "state.db"), clock=clock, sleep=clock.sleep)


def ctx(http: HttpClient, source: str, **kw: Any) -> FetchContext:
    return FetchContext(
        http=http,
        policy=RequestPolicy(source=source, daily_request_cap=100, requests_per_minute={}),
        now=T0,
        **kw,
    )


# --------------------------------------------------------------------------- Remote OK


def test_remoteok_parser_skips_legal_notice() -> None:
    data = body("remoteok")
    assert "legal" in data[0]
    jobs = remoteok.parse_response(data, FetchReport())
    assert len(jobs) == 3
    first = jobs[0]
    assert (first.source, first.source_job_id) == ("remoteok", "1137510")
    assert first.posting_url.startswith("https://remoteOK.com/remote-jobs/")
    assert first.apply_url is None
    assert first.published_at == datetime(2026, 10, 13, 8, 0, 11, tzinfo=UTC)
    assert normalise(first, []).remote_type is RemoteType.REMOTE


# --------------------------------------------------------------------------- Jobicy


def test_jobicy_parser() -> None:
    jobs = [jobicy.parse_job(r) for r in body("jobicy_page1")["jobs"]]
    first = jobs[0]
    assert first is not None
    assert (first.source_job_id, first.company) == ("155262", "MaintainX")
    assert first.posting_url == "https://jobicy.com/jobs/155262-software-developer-mobile-platform"
    job = normalise(first, [])
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.location_country == "CA"
    assert job.remote_type is RemoteType.REMOTE


@respx.mock
async def test_jobicy_cursor_paging(http: HttpClient) -> None:
    page1 = body("jobicy_page1")
    page2 = dict(page1, nextCursor=None, hasMore=False, jobs=[dict(page1["jobs"][0], id=999)])
    route = respx.get("https://jobicy.com/api/v2/remote-jobs")
    route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    c = ctx(
        http,
        "jobicy",
        queries=[{"geo": "europe", "industry": "engineering"}],
        options={"max_pages": 5},
    )
    jobs = await jobicy.JobicySource().fetch(c, SourceState("jobicy"))
    assert len(jobs) == 4
    first, second = (dict(call.request.url.params) for call in route.calls)
    assert first == {"count": "50", "geo": "europe", "industry": "engineering"}
    assert second["cursor"] == page1["nextCursor"]


# --------------------------------------------------------------------------- Himalayas


def test_himalayas_parser() -> None:
    record = body("himalayas_page1")["jobs"][0]
    job = himalayas.parse_job(record)
    assert job is not None
    assert job.source_job_id == "companies/fico/jobs/analytic-consultant-i"
    assert job.posting_url == "https://himalayas.app/companies/fico/jobs/analytic-consultant-i"
    assert job.published_at == datetime.fromtimestamp(1791988320, tz=UTC)
    normalised = normalise(job, ["FR"])
    assert normalised.remote_type is RemoteType.REMOTE
    assert normalised.countries == {"US"}
    worldwide = himalayas.parse_job(dict(record, locationRestrictions=[]))
    assert worldwide is not None and worldwide.locations == ["Worldwide"]


@respx.mock
async def test_himalayas_stops_at_max_pages(http: HttpClient) -> None:
    respx.get("https://himalayas.app/jobs/api").respond(json=body("himalayas_page1"))
    c = ctx(http, "himalayas", options={"max_pages": 2})
    jobs = await himalayas.HimalayasSource().fetch(c, SourceState("himalayas"))
    assert respx.calls.call_count == 2
    assert len(jobs) == 6
    assert respx.calls[1].request.url.params["cursor"]
    assert respx.calls[0].request.url.params["limit"] == "20"


# --------------------------------------------------------------------------- Arbeitnow


def test_arbeitnow_parser() -> None:
    jobs = [arbeitnow.parse_job(r) for r in body("arbeitnow_page1")["data"]]
    first = jobs[0]
    assert first is not None
    assert first.source_job_id == "head-of-growth-marketing-cologne-231596"
    assert first.posting_url.startswith("https://www.arbeitnow.com/jobs/")
    job = normalise(first, ["DE"])
    assert job.employment_type is EmploymentType.FULL_TIME  # "Unbefristeter arbeitsvertrag"
    assert (job.location_city, job.location_country) == ("Cologne", "DE")
    assert job.title_norm == "head of growth marketing"


@respx.mock
async def test_arbeitnow_stops_at_first_fully_known_page(http: HttpClient) -> None:
    page1 = body("arbeitnow_page1")
    respx.get("https://www.arbeitnow.com/api/job-board-api").respond(json=page1)
    known = {r["slug"] for r in page1["data"]}
    c = ctx(
        http,
        "arbeitnow",
        options={"max_pages": 10},
        is_known=lambda source, job_id: job_id in known,
    )
    jobs = await arbeitnow.ArbeitnowSource().fetch(c, SourceState("arbeitnow"))
    assert respx.calls.call_count == 1
    assert len(jobs) == 3


@respx.mock
async def test_arbeitnow_later_page_failure_keeps_earlier_jobs(http: HttpClient) -> None:
    route = respx.get("https://www.arbeitnow.com/api/job-board-api")
    route.side_effect = [httpx.Response(200, json=body("arbeitnow_page1")), httpx.Response(404)]
    c = ctx(http, "arbeitnow", options={"max_pages": 3})
    jobs = await arbeitnow.ArbeitnowSource().fetch(c, SourceState("arbeitnow"))
    assert len(jobs) == 3
    assert not c.report.complete


# --------------------------------------------------------------------------- Adzuna


def test_adzuna_parser() -> None:
    first, second = (adzuna.parse_job(r, "fr") for r in body("adzuna_fr_search")["results"])
    assert first is not None and second is not None
    assert first.posting_url.startswith("https://www.adzuna.fr/land/ad/4900112233")  # unresolved
    assert first.company == "Logistico SAS"
    job = normalise(first, ["FR"])
    assert job.title == "Développeur Python Junior H/F"
    assert job.title_norm == "developpeur python junior"
    assert (job.location_city, job.location_country) == ("Lyon", "FR")
    assert job.employment_type is EmploymentType.FULL_TIME
    stage = normalise(second, ["FR"])
    assert stage.employment_type is EmploymentType.INTERNSHIP  # from the title
    assert stage.location_city == "Paris"


@respx.mock
async def test_adzuna_keys_never_reach_cache_or_errors(http: HttpClient) -> None:
    route = respx.get("https://api.adzuna.com/v1/api/jobs/fr/search/1")
    route.side_effect = [httpx.Response(200, json=body("adzuna_fr_search"))] + [
        httpx.Response(500)
    ] * 3
    creds = {"ADZUNA_APP_ID": "id-123", "ADZUNA_APP_KEY": "key-SECRET"}
    c = ctx(
        http,
        "adzuna",
        queries=[{"country": "fr", "what": "python", "results_per_page": 2}],
        credentials=creds,
        options={"max_pages": 1},
    )
    jobs = await adzuna.AdzunaSource().fetch(c, SourceState("adzuna"))
    assert len(jobs) == 2
    params = dict(route.calls[0].request.url.params)
    assert params["app_key"] == "key-SECRET"
    assert params["what"] == "python"
    assert params["content-type"] == "application/json"
    c2 = ctx(http, "adzuna", queries=[{"country": "fr"}], credentials=creds)
    await adzuna.AdzunaSource().fetch(c2, SourceState("adzuna"))
    assert c2.report.errors and all("key-SECRET" not in e for e in c2.report.errors)
    import sqlite3

    rows = sqlite3.connect(http.store.path).execute("select url from http_validators").fetchall()
    assert all("key-SECRET" not in r[0] for r in rows)


# --------------------------------------------------------------------------- France Travail


def test_france_travail_parser() -> None:
    first, second = (
        france_travail.parse_job(r) for r in body("france_travail_search")["resultats"]
    )
    assert first is not None and second is not None
    assert first.posting_url == "https://candidat.francetravail.fr/offres/recherche/detail/201ABCD"
    assert first.apply_url == "https://jobs.logistico.fr/postuler/201ABCD"
    job = normalise(first, ["FR"])
    assert (job.location_city, job.location_country) == ("Paris", "FR")
    assert job.employment_type is EmploymentType.FULL_TIME
    alternance = normalise(second, ["FR"])
    assert alternance.employment_type is EmploymentType.APPRENTICESHIP
    assert alternance.location_city == "Lyon"
    assert second.posting_url.endswith("/201EFGH")  # built from the id when urlOrigine is absent
    assert second.company == "Entreprise non communiquée"


@pytest.mark.parametrize(
    ("libelle", "place"),
    [
        ("75 - Paris 8e Arrondissement", "Paris"),
        ("69 - LYON 03", "Lyon"),
        ("2A - Ajaccio", "Ajaccio"),
        ("13 - Marseille 1er Arrondissement", "Marseille"),
        ("Nantes", "Nantes"),
    ],
)
def test_france_travail_place(libelle: str, place: str) -> None:
    assert france_travail.clean_place(libelle) == place


@respx.mock
async def test_france_travail_token_cached_and_paging(http: HttpClient, clock: FakeClock) -> None:
    token = respx.post(france_travail.OAUTH_ENDPOINT).respond(json=body("france_travail_token"))
    search = respx.get(france_travail.SEARCH_URL)
    search.side_effect = [
        httpx.Response(206, json=body("france_travail_search")),
        httpx.Response(200, json=body("france_travail_search")),
        httpx.Response(204),
    ]
    source = france_travail.FranceTravailSource()
    creds = {"FRANCE_TRAVAIL_CLIENT_ID": "cid", "FRANCE_TRAVAIL_CLIENT_SECRET": "csecret"}
    c = ctx(
        http,
        "france_travail",
        queries=[{"motsCles": "python"}],
        credentials=creds,
        options={"max_pages": 3},
    )
    jobs = await source.fetch(c, SourceState("france_travail"))
    assert len(jobs) == 4  # 206 -> next page; 200 -> last page
    assert search.call_count == 2
    ranges = [call.request.url.params["range"] for call in search.calls]
    assert ranges == ["0-149", "150-299"]
    assert search.calls[0].request.headers["Authorization"] == "Bearer ft-test-token-123"
    form = token.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in form and "client_secret=csecret" in form
    assert token.calls[0].request.url.params["realm"] == "/partenaire"
    # Same token reused until it nearly expires.
    await source.fetch(
        ctx(http, "france_travail", credentials=creds), SourceState("france_travail")
    )
    assert token.call_count == 1
    clock.now += timedelta(seconds=1499)
    respx.get(france_travail.SEARCH_URL).respond(204)
    await source.fetch(
        ctx(http, "france_travail", credentials=creds), SourceState("france_travail")
    )
    assert token.call_count == 2


@respx.mock
async def test_france_travail_bad_credentials(http: HttpClient) -> None:
    respx.post(france_travail.OAUTH_ENDPOINT).respond(401, json={"error": "invalid_client"})
    with pytest.raises(SourceAuthError):
        await france_travail.FranceTravailSource().fetch(
            ctx(http, "france_travail"), SourceState("france_travail")
        )


# --------------------------------------------------------------------------- e-mail alerts


def load_eml(name: str) -> EmailMessage:
    message = email.message_from_bytes(
        (FIXTURES / "email" / name).read_bytes(), policy=email.policy.default
    )
    assert isinstance(message, EmailMessage)
    return message


def test_linkedin_alert_parser() -> None:
    sender, jobs = parse_message(load_eml("linkedin_alert.eml"))
    assert sender == "jobalerts-noreply@linkedin.com"
    assert [(j.title, j.company, j.locations) for j in jobs] == [
        ("Junior Python Developer", "Logistico", ["Tunis, Tunisia"]),
        ("Backend Engineer (Django)", "Acme SARL", ["Ariana, Tunisia (Hybrid)"]),
    ]
    assert jobs[0].source_job_id == "linkedin.com:3999000111"
    assert jobs[0].posting_url == "https://www.linkedin.com/comm/jobs/view/3999000111"
    assert jobs[0].published_at == datetime(2026, 10, 13, 7, 30, tzinfo=UTC)


def test_indeed_alert_parser() -> None:
    _, jobs = parse_message(load_eml("indeed_alert.eml"))
    assert [(j.title, j.company, j.source_job_id) for j in jobs] == [
        ("Développeur Python H/F", "DataCo", "indeed.com:0a1b2c3d4e5f6a7b"),
        ("Stage Data Engineer", "Startup SAS", "indeed.com:1122334455667788"),
    ]


def test_unknown_sender_yields_nothing() -> None:
    message = load_eml("linkedin_alert.eml")
    message.replace_header("From", "News <news@example.org>")
    assert parse_message(message)[1] == []


def test_every_supported_domain_has_a_parser() -> None:
    from jobfinder.config import SUPPORTED_ALERT_DOMAINS

    assert set(PARSERS) == set(SUPPORTED_ALERT_DOMAINS)


class FakeImap:
    def __init__(self, messages: dict[str, bytes], login_ok: bool = True) -> None:
        self.messages = messages
        self.login_ok = login_ok
        self.searches: list[tuple[str, ...]] = []
        self.seen: list[str] = []
        self.selected: tuple[str, bool] | None = None
        self.logged_out = False

    def login(self, user: str, password: str) -> Any:
        import imaplib

        if not self.login_ok:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b""]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> Any:
        self.selected = (mailbox, readonly)
        return "OK", [b"3"]

    def search(self, charset: str | None, *criteria: str) -> Any:
        self.searches.append(criteria)
        return "OK", [" ".join(self.messages).encode()]

    def fetch(self, message_set: str, message_parts: str) -> Any:
        assert message_parts == "(BODY.PEEK[])"  # reading must not mark as seen
        return "OK", [(b"1 (BODY[] {123}", self.messages[message_set]), b")"]

    def store(self, message_set: str, command: str, flags: str) -> Any:
        self.seen.append(message_set)
        return "OK", [b""]

    def logout(self) -> Any:
        self.logged_out = True
        return "BYE", [b""]


async def test_email_alerts_fetch(http: HttpClient) -> None:
    linkedin = (FIXTURES / "email" / "linkedin_alert.eml").read_bytes()
    spoofed = linkedin.replace(
        b"jobalerts-noreply@linkedin.com", b"jobalerts-noreply@linkedin.com.evil.test"
    )
    imap = FakeImap({"1": linkedin, "2": spoofed})
    source = EmailAlertsSource(imap_factory=lambda host: imap)
    c = ctx(
        http,
        "email_alerts",
        options={"allowed_senders": ["jobalerts-noreply@linkedin.com"]},
        credentials={"IMAP_HOST": "imap.test", "IMAP_USER": "u", "IMAP_PASSWORD": "p"},
    )
    jobs = await source.fetch(c, SourceState("email_alerts"))
    assert len(jobs) == 2
    assert imap.searches == [("UNSEEN", "FROM", '"jobalerts-noreply@linkedin.com"')]
    assert imap.selected == ("INBOX", False)
    assert imap.seen == ["1"]  # the spoofed sender is neither parsed nor marked
    assert imap.logged_out
    assert http.requests_by_source == {"email_alerts": 1}


async def test_email_alerts_login_failure_is_auth_error(http: HttpClient) -> None:
    source = EmailAlertsSource(imap_factory=lambda host: FakeImap({}, login_ok=False))
    c = ctx(http, "email_alerts", options={"allowed_senders": ["alert@indeed.com"]})
    with pytest.raises(SourceAuthError, match="IMAP login failed"):
        await source.fetch(c, SourceState("email_alerts"))


async def test_email_alerts_counts_against_cap(http: HttpClient) -> None:
    source = EmailAlertsSource(imap_factory=lambda host: FakeImap({}))
    c = FetchContext(
        http=http,
        now=T0,
        policy=RequestPolicy("email_alerts", 1, {}),
        options={"allowed_senders": ["alert@indeed.com"]},
    )
    await source.fetch(c, SourceState("email_alerts"))
    with pytest.raises(DailyCapReachedError):
        await source.fetch(c, SourceState("email_alerts"))
