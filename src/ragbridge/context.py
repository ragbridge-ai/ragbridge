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


def add_neighbours(
    rows: list[SearchResult],
    available: dict[tuple[uuid.UUID, int], Chunk],
    *,
    distance: int,
    max_chars: int,
) -> list[SearchResult]:
    """``rows`` plus the chunks up to ``distance`` before and after each of them.

    ``available`` holds chunks the caller fetched, keyed by ``(document_id,
    chunk_index)``. Neighbours come after the retrieved rows, so ranking and the
    retrieved chunks are untouched. They are added best-ranked chunk first, nearest
    first, and only while the context stays within ``max_chars`` characters: a
    local model has a small context window, and one that overflows loses the
    *start* of the prompt. The retrieved chunks themselves are always kept.
    """
    documents = {document.id: document for _, document, _ in rows}
    included = {(chunk.document_id, chunk.chunk_index) for chunk, _, _ in rows}
    total = sum(len(chunk.content) for chunk, _, _ in rows)

    added: list[SearchResult] = []
    for offset in range(1, distance + 1):
        for chunk, document, _ in rows:
            for index in (chunk.chunk_index - offset, chunk.chunk_index + offset):
                neighbour = available.get((document.id, index))
                if neighbour is None or (document.id, index) in included:
                    continue
                if total + len(neighbour.content) > max_chars:
                    continue
                included.add((document.id, index))
                total += len(neighbour.content)
                added.append((neighbour, documents[document.id], 0.0))
    return rows + added
