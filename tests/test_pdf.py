"""Unit tests for PDF text extraction. Pure functions, no database needed."""

import re

import pytest

from ragbridge.chunking import chunk_text
from ragbridge.pdf import extract_pdf_pages
from tests.helpers import build_pdf
from tests.pdf_fixtures import (
    ARTICLE_SENTENCES,
    CV_JOBS,
    ROTATED_NOTE,
    SINGLE_COLUMN_KINDS,
    build_pdf_with_rotated_note,
    build_single_column_pdf,
    build_two_column_article_pdf,
    build_two_column_cv_pdf,
)


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


# --- two-column pages -----------------------------------------------------------


def _dates_nearest_to_their_own_company(text: str) -> int:
    """How many date ranges are closer, by line, to their own company than to any other."""
    lines = text.split("\n")
    heading_lines = {i: h for i, line in enumerate(lines) for h, *_ in CV_JOBS if h in line}
    correct = 0
    for heading, dates, *_ in CV_JOBS:
        date_line = next(i for i, line in enumerate(lines) if dates in line)
        nearest = min((abs(i - date_line), h) for i, h in heading_lines.items())[1]
        correct += nearest == heading
    return correct


@pytest.mark.parametrize("stream_order", ["main_first", "side_first"])
def test_each_date_range_stays_with_its_own_company(stream_order: str) -> None:
    [text] = extract_pdf_pages(build_two_column_cv_pdf(stream_order))

    assert _dates_nearest_to_their_own_company(text) == len(CV_JOBS)


def test_plain_extraction_shows_the_column_mixing_this_avoids() -> None:
    """The reason for the default: read in file order, a side column's date and city
    land directly above the *next* company (three of the four here).
    """
    [text] = extract_pdf_pages(build_two_column_cv_pdf("main_first"), mode="plain")

    assert _dates_nearest_to_their_own_company(text) == 1


@pytest.mark.parametrize("stream_order", ["main_first", "side_first"])
def test_each_date_range_ends_up_in_the_same_chunk_as_its_company(stream_order: str) -> None:
    [text] = extract_pdf_pages(build_two_column_cv_pdf(stream_order))

    chunks = chunk_text(text, chunk_size=300, chunk_overlap=0)

    for heading, dates, _city, _ in CV_JOBS:
        [own] = [chunk for chunk in chunks if heading in chunk]
        assert dates in own, (heading, own)


def test_the_side_column_is_joined_to_its_company_row_without_layout_whitespace() -> None:
    [text] = extract_pdf_pages(build_two_column_cv_pdf())

    assert "Mar 2022 - present Company A - Senior Engineer" in text.split("\n")
    assert "  " not in text
    assert not any(line != line.strip() for line in text.split("\n"))
    assert "\n\n" not in text


@pytest.mark.parametrize("kind", SINGLE_COLUMN_KINDS)
def test_a_single_column_page_extracts_exactly_as_it_did_before(kind: str) -> None:
    data = build_single_column_pdf(kind)

    assert extract_pdf_pages(data) == extract_pdf_pages(data, mode="plain")


def test_a_page_of_two_full_text_columns_keeps_its_sentences_intact() -> None:
    """Reading such a page row by row would splice a sentence from each column."""
    data = build_two_column_article_pdf()

    [text] = extract_pdf_pages(data)

    flat = re.sub(r"\s+", " ", text)
    assert all(sentence in flat for sentence in ARTICLE_SENTENCES)
    assert extract_pdf_pages(data) == extract_pdf_pages(data, mode="plain")


def test_layout_mode_can_be_forced_and_splices_full_text_columns() -> None:
    [text] = extract_pdf_pages(build_two_column_article_pdf(), mode="layout")

    flat = re.sub(r"\s+", " ", text)
    assert sum(sentence in flat for sentence in ARTICLE_SENTENCES) == 0


def test_rotated_text_is_not_lost() -> None:
    """Layout extraction drops rotated text unless told not to; plain never did."""
    [text] = extract_pdf_pages(build_pdf_with_rotated_note())

    assert ROTATED_NOTE in text
    assert "Ordinary body text" in text
