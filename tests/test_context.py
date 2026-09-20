"""Tests for the context the answer model is given. Pure: chunks built in memory."""

import uuid

from ragbridge.chunking import chunk_text
from ragbridge.context import add_neighbours, build_context
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


# --- neighbours ------------------------------------------------------------------


def _available(
    document: Document, indexes: list[int], size: int = 10
) -> dict[tuple[uuid.UUID, int], Chunk]:
    """Chunks a database could hand back, each ``size`` characters long."""
    return {
        (document.id, index): Chunk(
            id=uuid.uuid4(),
            document_id=document.id,
            chunk_index=index,
            content=("x" * size),
            metadata_={},
        )
        for index in indexes
    }


def _indexes(rows: list[SearchResult]) -> list[int]:
    return [chunk.chunk_index for chunk, _, _ in rows]


def test_the_chunk_before_and_after_each_retrieved_chunk_is_added_after_the_retrieved_ones() -> (
    None
):
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 4, 0.9), _row(doc, 1, 0.8)]

    result = add_neighbours(
        retrieved, _available(doc, [0, 1, 2, 3, 4, 5]), distance=1, max_chars=10_000
    )

    assert _indexes(result) == [4, 1, 3, 5, 0, 2]
    assert result[:2] == retrieved


def test_a_chunk_that_was_already_retrieved_is_not_added_twice() -> None:
    doc = _document("cv.pdf")

    result = add_neighbours(
        [_row(doc, 1, 0.9), _row(doc, 2, 0.8)],
        _available(doc, [0, 1, 2, 3]),
        distance=1,
        max_chars=10_000,
    )

    assert sorted(_indexes(result)) == [0, 1, 2, 3]


def test_a_chunk_with_no_neighbour_on_one_side_only_gets_the_other() -> None:
    doc = _document("cv.pdf")

    result = add_neighbours([_row(doc, 0, 0.9)], _available(doc, [0, 1]), distance=1, max_chars=100)

    assert _indexes(result) == [0, 1]


def test_distance_zero_adds_nothing() -> None:
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 2, 0.9)]

    assert (
        add_neighbours(retrieved, _available(doc, [1, 2, 3]), distance=0, max_chars=100)
        == retrieved
    )


def test_neighbours_stop_when_the_character_budget_is_used_up() -> None:
    """Each chunk is 10 characters; the retrieved one plus one neighbour fill 20."""
    doc = _document("cv.pdf")

    result = add_neighbours(
        [_row(doc, 5, 0.9)], _available(doc, [4, 5, 6]), distance=1, max_chars=20
    )

    assert _indexes(result) == [5, 4]


def test_the_retrieved_chunks_are_kept_even_when_they_alone_exceed_the_budget() -> None:
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 5, 0.9, "y" * 50)]

    result = add_neighbours(retrieved, _available(doc, [4, 5, 6]), distance=1, max_chars=10)

    assert result == retrieved


def test_the_best_ranked_chunks_get_their_neighbours_first_under_a_tight_budget() -> None:
    doc = _document("cv.pdf")
    retrieved = [_row(doc, 8, 0.9), _row(doc, 2, 0.1)]

    result = add_neighbours(
        retrieved, _available(doc, [1, 2, 3, 7, 8, 9]), distance=1, max_chars=40
    )

    assert _indexes(result) == [8, 2, 7, 9]


def test_a_larger_distance_reaches_further() -> None:
    doc = _document("cv.pdf")

    result = add_neighbours(
        [_row(doc, 5, 0.9)], _available(doc, list(range(10))), distance=2, max_chars=10_000
    )

    assert sorted(_indexes(result)) == [3, 4, 5, 6, 7]


def test_neighbours_come_only_from_the_same_document() -> None:
    first, second = _document("a.md"), _document("b.md")
    available = {**_available(first, [1, 2, 3]), **_available(second, [1, 2, 3])}

    result = add_neighbours([_row(first, 2, 0.9)], available, distance=1, max_chars=1000)

    assert {chunk.document_id for chunk, _, _ in result} == {first.id}
