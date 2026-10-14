"""Removal of personal contact details from CV text before any AI call.

Heuristic by nature: tuned to remove emails, phone numbers and street addresses while
leaving dates, years, version numbers and ordinary sentences intact. Whatever an
address pattern matches is redacted to the end of its line (postcode and city).
"""

from __future__ import annotations

import re

EMAIL_MARK = "[EMAIL]"
PHONE_MARK = "[PHONE]"
ADDRESS_MARK = "[ADDRESS]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Candidate phone: optional +country/00 or (area), then digit groups joined by space,
# dot, dash or parentheses. Kept only with 8-15 digits and when not a date, a year
# range or a thousands-grouped number.
_PHONE = re.compile(r"(?<![\w/.,+-])(?:\+|00)?\(?\d[\d\s().-]{6,22}\d(?![\w/])")
_YEAR_RANGE = re.compile(r"^(19|20)\d{2}\s*[-.]\s*(19|20)\d{2}$")
_DATE_LIKE = re.compile(r"^\d{1,4}[.-]\d{1,2}[.-]\d{1,4}$")
_THOUSANDS = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")

# French-style, number then street type: "12 rue de la Paix", "5bis, Avenue Habib Bourguiba"
_FR_NUMBER_FIRST = re.compile(
    r"\b\d{1,5}\s?(?:bis|ter)?,?\s+(?:rue|avenue|av\.|boulevard|bd|chemin|allée|allee|"
    r"impasse|route)\b[^\n]*",
    re.IGNORECASE,
)
# English-style, number, capitalised name, street type: "221B Baker Street"
_EN_NUMBER_FIRST = re.compile(
    r"\b\d{1,5}[A-Za-z]?,?\s+(?:[A-Z][\w'\u2019.-]*\s+){0,3}"
    r"(?:Street|St\.|Road|Rd\.|Lane|Avenue|Ave\.|Boulevard|Blvd\.?|Drive|Dr\.|Way|Court|"
    r"Place|Square)(?=[\s,]|$)[^\n]*"
)
# Street type then number: "Hauptstraße 5", "Lindenweg 12a", "Rue de la Paix 12",
# "Avenue Habib Bourguiba 12, 1000 Tunis"
_STREET_FIRST = re.compile(
    r"(?:\b[A-ZÄÖÜ][\wäöüß]*(?:straße|strasse|str\.|weg|gasse|platz|allee)"
    r"|\b(?:[Rr]ue|[Aa]venue|[Bb]oulevard|[Cc]hemin|[Ii]mpasse))"
    r"\s+[^\n\d]{0,40}?\d{1,5}[a-z]?\b[^\n]*"
)
_LABELLED = re.compile(r"(?im)^(\s*(?:address|adresse|anschrift|domicile)\s*[:\-]\s*).+$")


def _phone_sub(match: re.Match[str]) -> str:
    candidate = match.group(0)
    stripped = candidate.strip()
    digits = sum(c.isdigit() for c in stripped)
    if (
        not 8 <= digits <= 15
        or _YEAR_RANGE.match(stripped)
        or _DATE_LIKE.match(stripped)
        or _THOUSANDS.match(stripped)
    ):
        return candidate
    trailing = candidate[len(candidate.rstrip()) :]
    return PHONE_MARK + trailing


def redact(text: str) -> str:
    """Replace emails, phone numbers and street addresses with placeholder tokens."""
    text = _EMAIL.sub(EMAIL_MARK, text)
    text = _LABELLED.sub(lambda m: m.group(1) + ADDRESS_MARK, text)
    for pattern in (_FR_NUMBER_FIRST, _EN_NUMBER_FIRST, _STREET_FIRST):
        text = pattern.sub(ADDRESS_MARK, text)
    return _PHONE.sub(_phone_sub, text)
