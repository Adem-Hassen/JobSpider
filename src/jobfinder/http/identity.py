"""How jobfinder identifies itself to remote services."""

from __future__ import annotations

from jobfinder import __version__


def user_agent(contact_email: str) -> str:
    return f"jobfinder/{__version__} (+mailto:{contact_email})"


# Sites that forbid automated access: never fetched, not even to resolve a redirect.
NEVER_FETCH_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com")


def never_fetch(host: str) -> bool:
    host = host.lower().rstrip(".")
    parts = host.split(".")
    # match the registrable name on any TLD: indeed.fr, uk.indeed.com, glassdoor.co.uk
    names = {d.split(".")[0] for d in NEVER_FETCH_DOMAINS}
    return any(part in names for part in parts[:-1])
