"""Build the context the answer model reads from the retrieved chunks.

Retrieval returns chunks best-first, which is the wrong order to *read* them
in. A chunk often ends with a heading whose bullets start the next one; read
in score order, the model can meet those bullets first, followed by the next
company's heading, and attach them to the wrong company. So the chunks are put
back in document order, each is labelled with where it came from, and a note
marks text that is missing between two chunks that were not neighbours.
"""

import uuid

from ragbridge.db.models import Chunk
from ragbridge.retrieval import SearchResult


def build_context(rows: list[SearchResult]) -> list[str]:
    """One entry per chunk, in the order the chunks appear in their documents.

    ``rows`` are the retrieved chunks, best first. A document's chunks are kept
    together in ``chunk_index`` order, and documents are ordered by their best
    chunk, so the most relevant document is read first. A chunk is labelled
    ``[filename, chunk N]`` with the same ``chunk_index`` the response reports
    for its source. When the previous chunk of the same document is not the
    one right before it, a note saying which chunks are not shown is put in
    front of it, inside the same entry, so there is still one entry per chunk.
    """
    best_rank: dict[uuid.UUID, int] = {}
    for rank, (_, document, _) in enumerate(rows):
        best_rank.setdefault(document.id, rank)
    ordered = sorted(rows, key=lambda row: (best_rank[row[1].id], row[0].chunk_index))

    context: list[str] = []
    previous: Chunk | None = None
    for chunk, document, _ in ordered:
        entry = f"[{document.filename}, chunk {chunk.chunk_index}]\n{chunk.content}"
        if (
            previous is not None
            and previous.document_id == chunk.document_id
            and chunk.chunk_index - previous.chunk_index > 1
        ):
            entry = f"{_gap_note(previous.chunk_index + 1, chunk.chunk_index - 1)}\n{entry}"
        context.append(entry)
        previous = chunk
    return context


def _gap_note(first: int, last: int) -> str:
    if first == last:
        return f"[... chunk {first} is not shown ...]"
    return f"[... chunks {first}-{last} are not shown ...]"
