"""Extract text from PDF files."""

import io
import re
from typing import Literal

from pypdf import PageObject, PdfReader
from pypdf.errors import PyPdfError

PdfExtraction = Literal["auto", "plain", "layout"]
"""How to read a page. See ``extract_pdf_pages``."""

_RUN_OF_SPACES = re.compile(r" {2,}")
_COLUMN_GAP = re.compile(r" {3,}")

TEXT_COLUMN_MIN_CHARS = 30
"""A fragment this long, on each side of a gap, is a column of prose."""
TEXT_COLUMN_MIN_ROWS = 3
"""How many rows of two long fragments make a page a page of text columns."""


def extract_pdf_pages(data: bytes, mode: PdfExtraction = "auto") -> list[str]:
    """Extract the text of each page in a PDF, in order.

    Returns one string per page, not one joined string, so a later step
    can record which page a chunk came from. A page with no extractable
    text (for example, a scanned image with no OCR layer) yields an
    empty string for that page, not an error.

    ``mode`` decides how each page is read:

    - ``"plain"``: pypdf's default, which returns text in the order the PDF
      was written. For a page with a narrow side column (a CV's dates and
      cities beside the job descriptions) that order is often wrong: a
      job's date and city land next to the *next* job.
    - ``"layout"``: read row by row, so a side column's text sits on the same
      line as the text beside it. Right for a side column, wrong for two
      columns of prose, where it splices a sentence from each column.
    - ``"auto"`` (default): per page, ``"layout"`` unless the page looks like
      two columns of prose, in which case ``"plain"`` - so when in doubt a page
      is read as it always was.

    Raises:
        ValueError: if ``data`` is not a readable PDF.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        return [_extract_page(page, mode) for page in reader.pages]
    except PyPdfError as exc:
        raise ValueError("could not read PDF") from exc


def _extract_page(page: PageObject, mode: PdfExtraction) -> str:
    if mode == "plain":
        return str(page.extract_text())
    rows = str(
        page.extract_text(
            extraction_mode="layout",
            # No blank lines for vertical gaps: pypdf's plain text has none either.
            layout_mode_space_vertically=False,
            # Layout mode drops rotated text by default; plain extraction never did.
            layout_mode_strip_rotated=False,
        )
    )
    if mode == "auto" and _has_text_columns(rows):
        return str(page.extract_text())
    return _tidy_layout(rows)


def _has_text_columns(rows: str) -> bool:
    """Whether the page has rows with two long fragments side by side.

    Layout mode separates columns by runs of spaces. A side column holds
    short labels (a date, a city) and is read well row by row; two columns of
    sentences are not.
    """
    long_rows = 0
    for row in rows.splitlines():
        fragments = [part for part in _COLUMN_GAP.split(row.strip()) if part]
        if len(fragments) >= 2 and min(map(len, fragments)) >= TEXT_COLUMN_MIN_CHARS:
            long_rows += 1
    return long_rows >= TEXT_COLUMN_MIN_ROWS


def _tidy_layout(rows: str) -> str:
    """Drop layout mode's indentation, column gaps and empty lines.

    Layout output pads every row with spaces to keep the page's geometry. Once
    the rows are read, that padding is noise for an embedding model, and with
    it removed a simple one-column page comes out exactly as plain extraction
    returns it.
    """
    lines = (_RUN_OF_SPACES.sub(" ", line).strip() for line in rows.splitlines())
    return "\n".join(line for line in lines if line)
