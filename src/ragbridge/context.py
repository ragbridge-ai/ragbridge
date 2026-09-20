"""Build the context the answer model reads from the retrieved chunks.

Retrieval returns chunks best-first, which is the wrong order to *read* them
in. A chunk often ends with a heading whose bullets start the next one, and read
as separate blocks the model attaches the last bullets of one company to the
heading that follows them. So the chunks are put back in document order, chunks
that were neighbours are stitched back into one continuous excerpt (the text
two neighbours share is written once), each excerpt is labelled with where it
came from, and a note marks text that is missing between two excerpts.
"""

import uuid

from ragbridge.db.models import Chunk, Document
from ragbridge.retrieval import SearchResult

MIN_OVERLAP = 20
"""Fewer shared characters at the seam of two neighbours are a coincidence, not overlap."""


def build_context(rows: list[SearchResult]) -> list[str]:
    """One entry per excerpt: a run of neighbouring chunks, in document order.

    ``rows`` are the chunks to show, best first. A document's chunks are kept
    together in ``chunk_index`` order, and documents are ordered by their best
    chunk, so the most relevant document is read first. Chunks with consecutive
    indices form one excerpt, labelled ``[filename, chunks 2-4]`` (or
    ``[filename, chunk 2]``) with the ``chunk_index`` values the response
    reports for its sources. When the previous excerpt of the same document is
    not the one right before it, a note saying which chunks are not shown is
    put in front of it, inside the same entry.
    """
    best_rank: dict[uuid.UUID, int] = {}
    for rank, (_, document, _) in enumerate(rows):
        best_rank.setdefault(document.id, rank)
    ordered = sorted(rows, key=lambda row: (best_rank[row[1].id], row[0].chunk_index))

    runs: list[list[tuple[Chunk, Document]]] = []
    for chunk, document, _ in ordered:
        if runs and _follows(runs[-1][-1][0], chunk):
            runs[-1].append((chunk, document))
        else:
            runs.append([(chunk, document)])

    context: list[str] = []
    previous_last: Chunk | None = None
    for run in runs:
        first, document = run[0]
        last = run[-1][0]
        span = (
            f"chunk {first.chunk_index}"
            if len(run) == 1
            else f"chunks {first.chunk_index}-{last.chunk_index}"
        )
        entry = f"[{document.filename}, {span}]\n{_stitch([chunk.content for chunk, _ in run])}"
        if previous_last is not None and previous_last.document_id == first.document_id:
            entry = f"{_gap_note(previous_last.chunk_index + 1, first.chunk_index - 1)}\n{entry}"
        context.append(entry)
        previous_last = last
    return context


def _follows(previous: Chunk, chunk: Chunk) -> bool:
    return previous.document_id == chunk.document_id and chunk.chunk_index == (
        previous.chunk_index + 1
    )


def _stitch(texts: list[str]) -> str:
    """Join neighbouring chunks into the text they were cut from.

    Chunks overlap, so the start of a chunk repeats the end of the one before it;
    that repeated text is written once. With no overlap to remove (or a
    coincidence shorter than ``MIN_OVERLAP``) the chunks are joined by a newline.
    """
    text = texts[0]
    for following in texts[1:]:
        shared = _overlap(text, following)
        text = f"{text}{following[shared:]}" if shared else f"{text}\n{following}"
    return text


def _overlap(text: str, following: str) -> int:
    """The length of the longest end of ``text`` that ``following`` starts with, or 0."""
    for length in range(min(len(text), len(following)), MIN_OVERLAP - 1, -1):
        if text.endswith(following[:length]):
            return length
    return 0


def _gap_note(first: int, last: int) -> str:
    if first == last:
        return f"[... chunk {first} is not shown ...]"
    return f"[... chunks {first}-{last} are not shown ...]"
