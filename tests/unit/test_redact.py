from __future__ import annotations

import pytest

from jobfinder.cv.redact import ADDRESS_MARK, EMAIL_MARK, PHONE_MARK, redact


@pytest.mark.parametrize(
    "email",
    ["amira@example.com", "a.b+jobs@sub.example.co.uk", "first_last-1@mail.tn"],
)
def test_emails(email: str) -> None:
    assert redact(f"Contact: {email}.") == f"Contact: {EMAIL_MARK}."


@pytest.mark.parametrize(
    "phone",
    [
        "+216 98 765 432",
        "+216 98765432",
        "0021698765432",
        "98 765 432",
        "06 12 34 56 78",
        "+33 6 12 34 56 78",
        "+33 (0)6 12 34 56 78",
        "(555) 123-4567",
        "555.123.4567",
        "+49 30 1234567",
        "+1-202-555-0143",
    ],
)
def test_phones(phone: str) -> None:
    assert redact(f"Tel: {phone}\nNext") == f"Tel: {PHONE_MARK}\nNext"


@pytest.mark.parametrize(
    "line",
    [
        "12 Avenue Habib Bourguiba, 1000 Tunis",
        "12 rue de la Paix, 75002 Paris",
        "5bis, Rue Ibn Khaldoun, Ariana",
        "221B Baker Street, London NW1 6XE",
        "10 Downing Street",
        "Hauptstraße 5, 10115 Berlin",
        "Lindenweg 12a, 80331 München",
        "Rue de la Liberté 14, 2000 Le Bardo",
    ],
)
def test_addresses(line: str) -> None:
    assert redact(f"Name\n{line}\nSkills") == f"Name\n{ADDRESS_MARK}\nSkills"


@pytest.mark.parametrize("label", ["Address", "Adresse", "Anschrift"])
def test_labelled_address(label: str) -> None:
    assert redact(f"{label}: Cité El Ghazala, Ariana") == f"{label}: {ADDRESS_MARK}"


@pytest.mark.parametrize(
    "text",
    [
        "2019 - 2021 Engineering degree",
        "2019-2021",
        "06/2025 - 09/2025 Backend intern",
        "12.03.2021",
        "2021-03-12",
        "Python 3.12.1 and PostgreSQL 16.2",
        "serving 1,200 users",
        "processed 10.000.000 events per day",
        "reduced costs by 40% in 2023",
        "GPA 3.85/4",
        "Communicated via Slack with 5 teams",
        "Built 3 REST APIs for Place reservations",
        "Planned 2 delivery route changes",
        "Led 3 teams to drive adoption",
        "Took first place in 2 hackathons",
        "ISO 27001 and ISO 9001 certified",
        "Avenue of growth",
        "Student ID 2021",
    ],
)
def test_false_positives_left_intact(text: str) -> None:
    assert redact(text) == text


def test_mixed_line_keeps_surrounding_text() -> None:
    text = "Email: a@b.com | Phone: +216 98 765 432 | Tunis"
    assert redact(text) == f"Email: {EMAIL_MARK} | Phone: {PHONE_MARK} | Tunis"
