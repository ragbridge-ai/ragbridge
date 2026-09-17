"""Unit tests for PDF text extraction. Pure functions, no database needed."""

import pytest

from ragbridge.pdf import extract_pdf_pages
from tests.helpers import build_pdf


def test_extract_pdf_pages_returns_one_string_per_page() -> None:
    data = build_pdf(["Hello, ragbridge.", "Second page."])

    result = extract_pdf_pages(data)

    assert result == ["Hello, ragbridge.", "Second page."]


def test_extract_pdf_pages_returns_empty_string_for_a_page_with_no_text() -> None:
    data = build_pdf([""])

    result = extract_pdf_pages(data)

    assert result == [""]


def test_extract_pdf_pages_rejects_data_that_is_not_a_pdf() -> None:
    with pytest.raises(ValueError, match="PDF"):
        extract_pdf_pages(b"not a pdf")
