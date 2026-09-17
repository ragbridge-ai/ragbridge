"""Extract text from PDF files."""

import io

from pypdf import PdfReader
from pypdf.errors import PyPdfError


def extract_pdf_pages(data: bytes) -> list[str]:
    """Extract the text of each page in a PDF, in order.

    Returns one string per page, not one joined string, so a later step
    can record which page a chunk came from. A page with no extractable
    text (for example, a scanned image with no OCR layer) yields an
    empty string for that page, not an error.

    Raises:
        ValueError: if ``data`` is not a readable PDF.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        return [page.extract_text() for page in reader.pages]
    except PyPdfError as exc:
        raise ValueError("could not read PDF") from exc
