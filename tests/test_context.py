"""Tests for the context the answer model is given. Pure: chunks built in memory."""

import uuid

from ragbridge.chunking import chunk_text
from ragbridge.context import build_context
from ragbridge.db.models import Chunk, Document
from ragbridge.retrieval import SearchResult


def _document(filename: str) -> Document:
    return Document(
        id=uuid.uuid4(), filename=filename, content_type="text/plain", sha256=filename, content="x"
    )


def _row(document: Document, index: int, score: float, text: str | None = None) -> SearchResult:
    chunk = Chunk(
        id=uuid.uuid4(),
        document_id=document.id,
        chunk_index=index,
        content=text or f"text {index}",
        metadata_={},
    )
    return chunk, document, score


def test_neighbouring_chunks_are_joined_into_one_excerpt_in_document_order() -> None:
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 2, 0.9), _row(doc, 0, 0.8), _row(doc, 1, 0.7)]

    assert build_context(retrieved) == ["[cv.pdf, chunks 0-2]\ntext 0\ntext 1\ntext 2"]


def test_a_single_chunk_is_labelled_with_its_own_index() -> None:
    doc = _document("cv.pdf")

    assert build_context([_row(doc, 3, 0.9)]) == ["[cv.pdf, chunk 3]\ntext 3"]


def test_a_gap_between_chunks_that_are_not_neighbours_is_marked() -> None:
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 4, 0.9), _row(doc, 0, 0.8), _row(doc, 1, 0.7)]

    assert build_context(retrieved) == [
        "[cv.pdf, chunks 0-1]\ntext 0\ntext 1",
        "[... chunks 2-3 are not shown ...]\n[cv.pdf, chunk 4]\ntext 4",
    ]


def test_a_single_missing_chunk_is_named_in_the_singular() -> None:
    doc = _document("cv.pdf")

    context = build_context([_row(doc, 0, 0.9), _row(doc, 2, 0.8)])

    assert context[1] == "[... chunk 1 is not shown ...]\n[cv.pdf, chunk 2]\ntext 2"


def test_neighbouring_chunks_have_no_gap_marker() -> None:
    doc = _document("cv.pdf")

    context = build_context([_row(doc, 3, 0.9), _row(doc, 2, 0.8)])

    assert not any("not shown" in block for block in context)


def test_a_document_is_kept_together_and_the_best_scoring_document_comes_first() -> None:
    first, second = _document("a.md"), _document("b.md")
    retrieved = [_row(second, 1, 0.9), _row(first, 0, 0.8), _row(second, 0, 0.7)]

    assert build_context(retrieved) == [
        "[b.md, chunks 0-1]\ntext 0\ntext 1",
        "[a.md, chunk 0]\ntext 0",
    ]


def test_a_new_document_does_not_get_a_gap_marker() -> None:
    first, second = _document("a.md"), _document("b.md")

    context = build_context([_row(first, 5, 0.9), _row(second, 0, 0.8)])

    assert context == ["[a.md, chunk 5]\ntext 5", "[b.md, chunk 0]\ntext 0"]


def test_the_input_is_left_alone() -> None:
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 4, 0.9), _row(doc, 0, 0.8)]
    before = list(retrieved)

    build_context(retrieved)

    assert retrieved == before


def test_no_chunks_give_no_context() -> None:
    assert build_context([]) == []


# --- stitching: the overlap between neighbours is not repeated -----------------


def test_the_text_two_neighbours_share_is_written_once() -> None:
    doc = _document("cv.pdf")
    first = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    second = "epsilon zeta eta theta iota kappa lambda mu"

    context = build_context([_row(doc, 0, 0.9, first), _row(doc, 1, 0.8, second)])

    assert context == [f"[cv.pdf, chunks 0-1]\n{first} lambda mu"]


def test_a_short_coincidence_at_the_seam_is_not_mistaken_for_overlap() -> None:
    """Both sides say "the": fewer than the minimum shared characters is not an overlap."""
    doc = _document("cv.pdf")

    context = build_context([_row(doc, 0, 0.9, "ends with the"), _row(doc, 1, 0.8, "the next one")])

    assert context == ["[cv.pdf, chunks 0-1]\nends with the\nthe next one"]


def test_stitching_the_chunks_of_a_paragraph_gives_the_paragraph_back() -> None:
    """The property that matters: overlapping chunks stitch to exactly the original text."""
    doc = _document("cv.pdf")
    original = " ".join(f"Sentence{n:03d} has alpha{n:03d} and beta{n:03d}." for n in range(1, 40))
    pieces = chunk_text(original, chunk_size=150, chunk_overlap=60)
    assert len(pieces) > 5
    rows = [_row(doc, index, 1.0 - index / 100, piece) for index, piece in enumerate(pieces)]

    [entry] = build_context(rows)

    assert entry == f"[cv.pdf, chunks 0-{len(pieces) - 1}]\n{original}"
