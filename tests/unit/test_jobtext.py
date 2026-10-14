from __future__ import annotations

from jobfinder.ai.jobtext import MAX_CHARS, prepare_job_text, strip_boilerplate, trim

POSTING = """About the role
Build REST APIs in Python.

Requirements:
- Python
- PostgreSQL

What we offer
- Free lunch
- Gym

About Acme
Acme was founded in 1999 and loves customers.

Nice to have
- Docker

Acme is an equal opportunity employer and considers all applicants without regard to race.
Wir bieten
- Obstkorb
"""


def test_boilerplate_sections_removed() -> None:
    text = strip_boilerplate(POSTING)
    assert "Build REST APIs in Python." in text
    assert "- PostgreSQL" in text
    assert "Nice to have\n- Docker" in text
    for gone in ("Free lunch", "founded in 1999", "equal opportunity", "Obstkorb"):
        assert gone not in text


def test_trim_at_paragraph_boundary() -> None:
    text = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(40))
    out = trim(text)
    assert len(out) <= MAX_CHARS + 10
    assert out.endswith("[...]")
    assert out.split("\n\n")[-1].startswith("Paragraph") or out.endswith("x\n[...]")


def test_short_text_unchanged() -> None:
    assert prepare_job_text("Python developer.") == "Python developer."
