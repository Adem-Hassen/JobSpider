"""Job text sent to the model: boilerplate removed, trimmed to about MAX_TOKENS tokens."""

from __future__ import annotations

import re

from jobfinder.pipeline.countries import fold

MAX_TOKENS = 1200
CHARS_PER_TOKEN = 4  # rough average for English/French/German prose
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN
_HEADING_MAX_CHARS = 70

# Section headings whose content does not help judge fit.
_BOILERPLATE_HEADINGS = re.compile(
    r"^(?:what we offer|what you(?:'| wi)ll get|our offer|benefits?|perks|"
    r"(?:benefits|perks) (?:and|&) (?:perks|benefits)|why (?:join|work)|why us|"
    r"about (?:us|the company|the team|[a-z0-9 .&-]{1,30})$|who we are|life at|"
    r"our (?:values|culture|mission)|"
    r"equal (?:employment )?opportunit|diversity|eeo|privacy|data protection|"
    r"how to apply|application process|our hiring process|interview process|"
    r"avantages|nous offrons|ce que nous offrons|qui sommes-nous|a propos|"
    r"wir bieten|was wir bieten|benefits bei|uber uns)",
)
_RELEVANT_HEADINGS = re.compile(
    r"(?:responsibilit|requirement|qualification|what you(?:'| wi)ll do|you will|your role|"
    r"the role|about the (?:role|job|position)|skills|experience|must have|nice to have|"
    r"profil|missions?|aufgaben|anforderungen|dein profil|ihr profil|tasks)",
)
_BOILERPLATE_LINES = re.compile(
    r"(?:equal opportunity employer|without regard to (?:race|age)|reasonable accommodation|"
    r"e-verify|privacy (?:notice|policy)|by applying|recruitment agencies|"
    r"we do not accept unsolicited)",
)


def _is_heading(line: str) -> bool:
    stripped = line.strip().rstrip(":")
    return (
        0 < len(stripped) <= _HEADING_MAX_CHARS
        and not stripped.startswith(("-", "*", "\u2022", "\u2013"))  # list items
        and not stripped.endswith((".", ",", ";"))
    )


def strip_boilerplate(text: str) -> str:
    """Drop boilerplate sections (from their heading to the next heading) and lines."""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        folded = fold(line).strip().lstrip("#*- ").rstrip(":").strip()
        if folded and _is_heading(line):
            skipping = bool(_BOILERPLATE_HEADINGS.match(folded)) and not _RELEVANT_HEADINGS.search(
                folded
            )
        if skipping or _BOILERPLATE_LINES.search(folded):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def trim(text: str, max_chars: int = MAX_CHARS) -> str:
    """Cut at a paragraph or line boundary near ``max_chars``."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = max(cut.rfind("\n\n"), cut.rfind("\n"))
    if boundary > max_chars * 0.6:
        cut = cut[:boundary]
    return cut.rstrip() + "\n[...]"


def prepare_job_text(description: str) -> str:
    return trim(strip_boilerplate(description))
