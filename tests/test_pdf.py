"""Unit tests for PDF text extraction. Pure functions, no database needed."""

import pytest
from fpdf import FPDF

from ragbridge.pdf import extract_pdf_pages


def _build_pdf(pages: list[str]) -> bytes:
    """Build a real PDF with one page per string, using fpdf2.

    Reading a PDF actually produced by a PDF library is a better test
    than hand-written PDF byte literals, which are brittle and hard to
    read in a diff.
    """
    pdf = FPDF()
    for text in pages:
        pdf.add_page()
        if text:
            pdf.set_font("Helvetica", size=12)
            pdf.cell(text=text)
    return bytes(pdf.output())


def test_extract_pdf_pages_returns_one_string_per_page() -> None:
    data = _build_pdf(["Hello, ragbridge.", "Second page."])

    result = extract_pdf_pages(data)

    assert result == ["Hello, ragbridge.", "Second page."]


def test_extract_pdf_pages_returns_empty_string_for_a_page_with_no_text() -> None:
    data = _build_pdf([""])

    result = extract_pdf_pages(data)

    assert result == [""]


def test_extract_pdf_pages_rejects_data_that_is_not_a_pdf() -> None:
    with pytest.raises(ValueError, match="PDF"):
        extract_pdf_pages(b"not a pdf")
