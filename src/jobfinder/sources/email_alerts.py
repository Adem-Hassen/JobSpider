"""Job alert e-mails over IMAP.

Only unseen messages from ``allowed_senders`` are read. Each supported sender domain
has a parser that recognises that site's job links in the HTML body and takes the link
text as the title and the following text blocks as company and location. Links are
stored exactly as found and are never fetched (LinkedIn, Indeed and Glassdoor forbid
automated access). A message is marked seen only after it was parsed.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import hashlib
import imaplib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any, Protocol

from jobfinder.config import EmailAlertsOptions, alert_domain
from jobfinder.http.client import FetchError, SourceAuthError
from jobfinder.models import RawJob
from jobfinder.pipeline.text import unescape_fully
from jobfinder.sources.base import FetchContext
from jobfinder.storage.state_store import SourceState

IMAP_TIMEOUT_S = 30


# --------------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class AlertJob:
    title: str
    company: str | None
    location: str | None
    url: str
    job_id: str


class _Tokens(HTMLParser):
    """Flattens HTML into ("link", href, text) and ("text", value) tokens."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            self.tokens.append(("link", self._href, " ".join(self._link_text).strip()))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._href is not None:
            self._link_text.append(value)
        else:
            self.tokens.append(("text", "", value))


@dataclass(frozen=True)
class AlertParser:
    domain: str
    link: re.Pattern[str]  # must define a named group "id" where the site has one
    ignore_texts: frozenset[str] = frozenset()

    def _job_id(self, match: re.Match[str], url: str) -> str:
        found = match.groupdict().get("id")
        return found or hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()[:16]

    def parse(self, html: str) -> list[AlertJob]:
        tokens = _Tokens()
        tokens.feed(html)
        tokens.close()
        found: dict[str, AlertJob] = {}
        stream = tokens.tokens
        for index, (kind, href, value) in enumerate(stream):
            if kind != "link":
                continue
            href = unescape_fully(href)
            match = self.link.search(href)
            if match is None:
                continue
            job_id = self._job_id(match, href)
            following: list[str] = []
            for next_kind, next_href, next_value in stream[index + 1 :]:
                if next_kind == "link" and self.link.search(unescape_fully(next_href)):
                    break
                if next_kind == "text" and next_value.lower() not in self.ignore_texts:
                    following.append(next_value)
                if len(following) == 2:
                    break
            title = value.strip()
            previous = found.get(job_id)
            if not title or (previous is not None and len(previous.title) >= len(title)):
                continue  # logo links and duplicates: keep the most descriptive anchor
            found[job_id] = AlertJob(
                title=title,
                company=following[0] if following else None,
                location=following[1] if len(following) > 1 else None,
                url=match.group(0),
                job_id=job_id,
            )
        return list(found.values())


PARSERS: dict[str, AlertParser] = {
    "linkedin.com": AlertParser(
        "linkedin.com",
        re.compile(
            r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(?P<id>\d+)"
        ),
        frozenset({"apply", "easy apply", "view job", "promoted", "actively recruiting"}),
    ),
    "indeed.com": AlertParser(
        "indeed.com",
        re.compile(
            r"https?://(?:[a-z]{2}\.)?(?:www\.)?indeed\.[a-z.]+/(?:rc/clk|viewjob|pagead/clk|m/viewjob)"
            r"[^\s\"'<>]*?[?&]jk=(?P<id>[0-9a-f]{8,})"
        ),
        frozenset({"easily apply", "new", "urgently hiring"}),
    ),
    "welcometothejungle.com": AlertParser(
        "welcometothejungle.com",
        re.compile(
            r"https?://(?:www\.)?welcometothejungle\.com/[a-z]{2}/companies/[^/\s\"']+/jobs/"
            r"(?P<id>[^/?#\s\"']+)"
        ),
    ),
    "glassdoor.com": AlertParser(
        "glassdoor.com",
        re.compile(
            r"https?://(?:www\.)?glassdoor\.[a-z.]+/(?:partner/jobListing|job-listing)[^\s\"'<>]*?"
            r"(?:jobListingId|jl)=(?P<id>\d+)"
        ),
    ),
}


def html_body(message: EmailMessage) -> str | None:
    part = message.get_body(preferencelist=("html",))
    if part is None:
        return None
    content = part.get_content()
    return content if isinstance(content, str) else None


def parse_message(message: EmailMessage) -> tuple[str | None, list[RawJob]]:
    """(sender address, jobs) for one alert e-mail."""
    sender = email.utils.parseaddr(str(message.get("From", "")))[1].lower() or None
    domain = alert_domain(sender) if sender else None
    if sender is None or domain is None:
        return sender, []
    html = html_body(message)
    if html is None:
        return sender, []
    published = None
    if message.get("Date"):
        try:
            published = email.utils.parsedate_to_datetime(str(message["Date"])).astimezone(UTC)
        except (TypeError, ValueError):
            published = None
    jobs = [
        RawJob(
            source="email_alerts",
            source_job_id=f"{domain}:{alert.job_id}",
            is_ats=False,
            title=alert.title,
            company=alert.company or "Unknown company",
            locations=[alert.location] if alert.location else [],
            posting_url=alert.url,  # stored as found, never fetched
            published_at=published,
            tags=[domain],
        )
        for alert in PARSERS[domain].parse(html)
    ]
    return sender, jobs


# --------------------------------------------------------------------------- IMAP


class ImapClient(Protocol):
    def login(self, user: str, password: str) -> Any: ...
    def select(self, mailbox: str = ..., readonly: bool = ...) -> Any: ...
    def search(self, charset: str | None, *criteria: str) -> Any: ...
    def fetch(self, message_set: str, message_parts: str) -> Any: ...
    def store(self, message_set: str, command: str, flags: str) -> Any: ...
    def logout(self) -> Any: ...


def _default_imap(host: str) -> ImapClient:
    return imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT_S)


@dataclass
class EmailAlertsSource:
    name: str = "email_alerts"
    imap_factory: Callable[[str], ImapClient] = field(default=_default_imap)

    def _read(
        self, host: str, user: str, password: str, options: EmailAlertsOptions
    ) -> tuple[list[RawJob], list[str]]:
        try:
            client = self.imap_factory(host)
        except OSError as exc:
            raise FetchError(f"email_alerts: cannot connect to {host}: {exc}") from exc
        jobs: list[RawJob] = []
        notes: list[str] = []
        try:
            try:
                client.login(user, password)
            except imaplib.IMAP4.error as exc:
                raise SourceAuthError(f"email_alerts: IMAP login failed for {user}", 401) from exc
            status, _ = client.select(options.mailbox, readonly=not options.mark_seen)
            if status != "OK":
                raise FetchError(f"email_alerts: cannot open mailbox {options.mailbox!r}")
            budget = options.max_messages
            for sender in options.allowed_senders:
                if budget <= 0:
                    break
                status, data = client.search(None, "UNSEEN", "FROM", f'"{sender}"')
                if status != "OK":
                    notes.append(f"search failed for {sender}")
                    continue
                ids: Sequence[bytes] = (data[0] or b"").split()[:budget]
                budget -= len(ids)
                for message_id in ids:
                    status, parts = client.fetch(message_id.decode(), "(BODY.PEEK[])")
                    raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                    if status != "OK" or raw is None:
                        notes.append(f"cannot fetch message {message_id.decode()}")
                        continue
                    message = email.message_from_bytes(raw, policy=email.policy.default)
                    if not isinstance(message, EmailMessage):  # pragma: no cover
                        continue
                    actual, found = parse_message(message)
                    if actual != sender:  # IMAP FROM matches substrings; be exact
                        continue
                    if not found:
                        notes.append(f"no jobs recognised in a message from {sender}")
                        continue
                    jobs.extend(found)
                    if options.mark_seen:
                        client.store(message_id.decode(), "+FLAGS", "\\Seen")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise FetchError(f"email_alerts: IMAP error: {exc}") from exc
        finally:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                client.logout()
        return jobs, notes

    async def fetch(self, ctx: FetchContext, state: SourceState) -> list[RawJob]:
        options = EmailAlertsOptions.model_validate(ctx.options)
        ctx.http.reserve(ctx.policy)  # one IMAP session counts as one request
        jobs, notes = await asyncio.to_thread(
            self._read,
            ctx.credentials.get("IMAP_HOST", ""),
            ctx.credentials.get("IMAP_USER", ""),
            ctx.credentials.get("IMAP_PASSWORD", ""),
            options,
        )
        ctx.report.skipped_records += len(notes)
        return jobs
