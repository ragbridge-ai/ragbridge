"""Split text into overlapping chunks for embedding.

Paragraph-first: blank lines split the text into paragraphs, and each
paragraph becomes its own chunk. A paragraph longer than ``chunk_size`` is
further split by characters, with ``chunk_overlap`` characters repeated
between consecutive chunks so a sentence split across the boundary is not
lost entirely.
"""

import re

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


def chunk_text(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split ``text`` into paragraph- or character-sized chunks.

    Raises:
        ValueError: if ``chunk_overlap`` is not smaller than ``chunk_size``
            - that combination would either loop forever or never move
            forward through a long paragraph.
    """
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    paragraphs = [paragraph.strip() for paragraph in _PARAGRAPH_SPLIT.split(text)]

    chunks: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if len(paragraph) <= chunk_size:
            chunks.append(paragraph)
        else:
            chunks.extend(_split_by_characters(paragraph, chunk_size, chunk_overlap))
    return chunks


def _split_by_characters(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split ``text`` into fixed-size, overlapping character windows."""
    step = chunk_size - chunk_overlap
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += step
    return chunks
