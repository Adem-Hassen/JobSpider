from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder.cv.extract import CVError, extract_text, normalise_text
from tests.helpers import make_docx, make_pdf, sample_cv_lines


def test_pdf(tmp_path: Path) -> None:
    text = extract_text(make_pdf(tmp_path / "cv.pdf", sample_cv_lines()))
    assert "Junior Backend Developer" in text
    assert "Arabic (native), French (C1)" in text


def test_docx_with_table(tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "cv.DOCX",
        sample_cv_lines(),
        table=[["Skill", "Level"], ["Kubernetes", "Basic"]],
    )
    text = extract_text(path)
    assert "INSAT" in text
    assert "Kubernetes | Basic" in text


def test_too_little_text_fails_clearly(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "scan.pdf", ["Amira"])
    with pytest.raises(CVError, match="Scanned or image-only"):
        extract_text(path)


def test_empty_docx(tmp_path: Path) -> None:
    with pytest.raises(CVError, match="only 0 characters"):
        extract_text(make_docx(tmp_path / "cv.docx", []))


def test_corrupt_pdf(tmp_path: Path) -> None:
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"not a pdf at all")
    with pytest.raises(CVError, match="not a readable PDF"):
        extract_text(path)


def test_corrupt_docx(tmp_path: Path) -> None:
    path = tmp_path / "cv.docx"
    path.write_bytes(b"PK\x03\x04 broken zip")
    with pytest.raises(CVError, match="not a readable DOCX"):
        extract_text(path)


def test_unsupported_format(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("x" * 500)
    with pytest.raises(CVError, match=r"unsupported format \.txt"):
        extract_text(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CVError, match="file not found"):
        extract_text(tmp_path / "nope.pdf")


def test_normalise_text() -> None:
    assert normalise_text("a  \r\nb\r\n\r\n\r\n\r\nc\x00") == "a\nb\n\nc"
