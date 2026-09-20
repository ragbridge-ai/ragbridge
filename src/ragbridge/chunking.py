"""Split text into overlapping chunks for embedding.

Paragraph-first: blank lines split the text into paragraphs, and a paragraph
that fits in ``chunk_size`` becomes one chunk. A longer paragraph is cut at
the coarsest boundary that keeps the pieces small enough - a line end, then a
sentence end, then a space - and the pieces are packed into chunks of at most
``chunk_size`` characters. The ``chunk_overlap`` between neighbours is made of
whole pieces too, so no chunk ever starts or ends inside a word.

The one unavoidable exception is a single word longer than ``chunk_size``
(a very long URL, say): with no boundary to use, it is cut into character
windows, as it always was.

A chunk shorter than ``min_size`` (a name, a city, a lone URL) says almost
nothing on its own and still competes with real content in search, so it is
merged into its neighbour.

A markdown heading belongs with the text below it. No chunk ends on one: the
heading moves to the start of the next chunk, so the words that describe a
section are embedded and retrieved with its text. A carried heading can make a
chunk longer than ``chunk_size`` by the length of the heading.
"""

import re

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_HEADING_LINE = re.compile(r"^#{1,6} \S")
_BOUNDARIES = (
    re.compile(r"\n"),
    re.compile(r"(?<=[.!?])\s+"),
    re.compile(r"\s+"),
)
"""Where a paragraph may be cut, coarsest first: lines, sentences, words."""

_Piece = tuple[str, bool]
"""A piece of text and whether it is already a finished chunk (a character window)."""


def chunk_text(text: str, *, chunk_size: int, chunk_overlap: int, min_size: int = 0) -> list[str]:
    """Split ``text`` into chunks of at most ``chunk_size`` characters.

    Raises:
        ValueError: if ``chunk_overlap`` is not smaller than ``chunk_size``
            - that combination would either loop forever or never move
            forward through a long paragraph - or if ``min_size`` is not
            smaller than ``chunk_size``.
    """
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    if min_size >= chunk_size:
        raise ValueError("min_size must be smaller than chunk_size")

    paragraphs = [paragraph.strip() for paragraph in _PARAGRAPH_SPLIT.split(text)]

    chunks: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if len(paragraph) <= chunk_size:
            chunks.append(paragraph)
        else:
            pieces = _pieces(paragraph, chunk_size, chunk_overlap, level=0)
            chunks.extend(_pack(pieces, chunk_size, chunk_overlap))
    chunks = _carry_headings(chunks, chunk_overlap)
    return _merge_small(chunks, min_size) if min_size > 0 else chunks


def _pieces(text: str, chunk_size: int, chunk_overlap: int, level: int) -> list[_Piece]:
    """Cut ``text`` into pieces of at most ``chunk_size`` characters.

    Each piece ends where ``_BOUNDARIES[level]`` (or a finer one) matches, and
    keeps the separator that follows it, so joining consecutive pieces gives
    back a contiguous stretch of the original text.
    """
    if level == len(_BOUNDARIES):
        windows = _split_by_characters(text.strip(), chunk_size, chunk_overlap)
        return [(window, True) for window in windows]

    pieces: list[_Piece] = []
    for piece in _cut(text, _BOUNDARIES[level]):
        if len(piece) <= chunk_size:
            pieces.append((piece, False))
        else:
            pieces.extend(_pieces(piece, chunk_size, chunk_overlap, level + 1))
    return pieces


def _cut(text: str, boundary: re.Pattern[str]) -> list[str]:
    """Split ``text`` after every match of ``boundary``, keeping the separators."""
    parts: list[str] = []
    start = 0
    for match in boundary.finditer(text):
        parts.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        parts.append(text[start:])
    return parts


def _pack(pieces: list[_Piece], chunk_size: int, chunk_overlap: int) -> list[str]:
    """Pack pieces into chunks, starting each one with the previous chunk's tail."""
    chunks: list[str] = []
    window: list[str] = []
    length = 0
    for text, finished in pieces:
        if finished:
            _emit(chunks, window)
            window, length = [], 0
            chunks.append(text)
            continue
        if window and length + len(text) > chunk_size:
            previous = "".join(window)
            _emit(chunks, window)
            tail = _tail(previous, chunk_overlap)
            window = [tail] if tail and len(tail) + len(text) <= chunk_size else []
            length = len(window[0]) if window else 0
        window.append(text)
        length += len(text)
    _emit(chunks, window)
    return chunks


def _emit(chunks: list[str], window: list[str]) -> None:
    chunk = "".join(window).strip()
    if chunk:
        chunks.append(chunk)


def _tail(text: str, chunk_overlap: int) -> str:
    """The end of ``text``, at most ``chunk_overlap`` characters, from a boundary.

    The earliest line start inside that stretch, else the earliest sentence
    start, else the earliest word start: as much context as fits, without
    beginning in the middle of a word. Empty when the stretch is a single
    word fragment. The separator that followed the text is kept, so the next
    piece does not get glued to it.
    """
    body = text.rstrip()
    separator = text[len(body) :]
    start = len(body) - chunk_overlap
    if chunk_overlap <= 0 or start <= 0:
        return ""
    region = body[start:]
    if body[start - 1].isspace():
        return region + separator  # the stretch already begins at the start of a word
    for boundary in _BOUNDARIES:
        match = boundary.search(region)
        if match:
            return region[match.end() :] + separator
    return ""


def _carry_headings(chunks: list[str], chunk_overlap: int) -> list[str]:
    """Move the heading lines a chunk ends with to the start of the next chunk.

    Skipped for the last chunk, which has nothing after it, and for a heading the next
    chunk already starts with because the overlap repeated it. A chunk that was only
    headings disappears: they now lead the next one.
    """
    carried = list(chunks)
    for index in range(len(carried) - 1):
        rest, headings = _split_trailing_headings(carried[index])
        if not headings:
            continue
        carried[index] = rest
        following = carried[index + 1]
        if headings not in following[: chunk_overlap + len(headings)]:
            carried[index + 1] = f"{headings}\n\n{following}"
    return [chunk for chunk in carried if chunk.strip()]


def _split_trailing_headings(chunk: str) -> tuple[str, str]:
    """``(text before, trailing heading lines)``; the second is empty when there are none."""
    lines = chunk.rstrip().split("\n")
    start = len(lines)
    for position in range(len(lines) - 1, -1, -1):
        if not lines[position].strip():
            continue  # a blank line between headings does not end the run
        if _HEADING_LINE.match(lines[position]):
            start = position
        else:
            break
    if start == len(lines):
        return chunk, ""
    return "\n".join(lines[:start]).rstrip(), "\n".join(lines[start:]).strip()


def _merge_small(chunks: list[str], min_size: int) -> list[str]:
    """Merge every chunk shorter than ``min_size`` into its neighbour.

    A short chunk joins the one after it; one that a heading separates from what
    follows (``_short_before_a_heading``), and a short *last* chunk, join the one
    before it. A merged chunk can be longer than ``chunk_size`` by less than
    ``min_size``. A document that is a single short chunk stays as it is: there is
    no neighbour.
    """
    merged: list[str] = []
    for position, chunk in enumerate(chunks):
        if merged and len(merged[-1]) < min_size:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        elif _short_before_a_heading(chunks, position, min_size) and merged:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)
    if len(merged) > 1 and len(merged[-1]) < min_size:
        last = merged.pop()
        merged[-1] = f"{merged[-1]}\n\n{last}"
    return merged


def _short_before_a_heading(chunks: list[str], position: int, min_size: int) -> bool:
    """A short chunk that the next chunk's heading separates from what follows it.

    It ends the section above the heading, so it joins the chunk before it, not the one
    after: merging forward would carry it across the heading into the wrong section.
    """
    following = chunks[position + 1] if position + 1 < len(chunks) else ""
    return len(chunks[position]) < min_size and bool(_HEADING_LINE.match(following))


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
