"""Unit tests for the chunker. Pure functions, no database needed."""

import pytest

from ragbridge.chunking import chunk_text


def test_chunk_text_returns_empty_list_for_empty_text() -> None:
    assert chunk_text("", chunk_size=100, chunk_overlap=10) == []


def test_chunk_text_returns_one_chunk_for_short_text() -> None:
    assert chunk_text("Hello, ragbridge.", chunk_size=100, chunk_overlap=10) == [
        "Hello, ragbridge."
    ]


def test_chunk_text_returns_one_chunk_per_short_paragraph() -> None:
    text = "Para one.\n\nPara two.\n\nPara three."

    result = chunk_text(text, chunk_size=100, chunk_overlap=10)

    assert result == ["Para one.", "Para two.", "Para three."]


def test_chunk_text_splits_a_long_paragraph_by_characters_with_overlap() -> None:
    text = "abcdefghijklmnopqrstuvwxyz"

    result = chunk_text(text, chunk_size=10, chunk_overlap=3)

    assert result == ["abcdefghij", "hijklmnopq", "opqrstuvwx", "vwxyz"]


def test_chunk_text_rejects_overlap_not_smaller_than_size() -> None:
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text("some text", chunk_size=10, chunk_overlap=10)


# --- word boundaries ---------------------------------------------------------


def _sentences(count: int) -> str:
    """Invented text with many distinct, easy-to-spot words: 'alpha001', 'alpha002', ..."""
    return " ".join(
        f"Sentence{n:03d} has alpha{n:03d} and beta{n:03d}." for n in range(1, count + 1)
    )


def test_no_chunk_starts_or_ends_inside_a_word() -> None:
    """The old character windows cut ``the`` into ``he``. Every token in every chunk,
    including the first and last of the overlap, must be a whole token of the text.
    """
    text = _sentences(60)
    words = set(text.split())

    chunks = chunk_text(text, chunk_size=120, chunk_overlap=30)

    assert len(chunks) > 5
    for chunk in chunks:
        assert set(chunk.split()) <= words, chunk


def test_every_word_of_the_text_is_in_some_chunk_and_the_order_is_kept() -> None:
    text = _sentences(40)

    chunks = chunk_text(text, chunk_size=100, chunk_overlap=25)

    assert chunks[0].split()[0] == text.split()[0]
    assert chunks[-1].split()[-1] == text.split()[-1]
    covered = " ".join(chunks).split()
    assert set(text.split()) <= set(covered)
    # each chunk begins no earlier than the previous one: the text is walked once
    positions = [text.find(chunk) for chunk in chunks]
    assert positions == sorted(positions)
    assert -1 not in positions


def test_no_chunk_is_longer_than_chunk_size() -> None:
    chunks = chunk_text(_sentences(50), chunk_size=90, chunk_overlap=20)

    assert max(len(chunk) for chunk in chunks) <= 90


def _shared_length(previous: str, following: str) -> int:
    """The longest end of ``previous`` that ``following`` starts with."""
    return max(k for k in range(len(following) + 1) if previous.endswith(following[:k]))


def test_the_overlap_starts_at_a_word_and_never_exceeds_the_setting() -> None:
    chunks = chunk_text(_sentences(30), chunk_size=100, chunk_overlap=30)

    assert len(chunks) > 5
    for previous, following in zip(chunks, chunks[1:], strict=False):
        shared = _shared_length(previous, following)
        assert 0 < shared <= 30, (previous, following)
        assert previous[len(previous) - shared - 1].isspace(), "the overlap starts mid-word"


def test_the_overlap_works_when_every_sentence_is_longer_than_it() -> None:
    """The first version built the overlap from whole sentences and, when none fitted,
    silently had no overlap at all.
    """
    chunks = chunk_text(_sentences(30), chunk_size=100, chunk_overlap=10)

    assert any(_shared_length(a, b) > 0 for a, b in zip(chunks, chunks[1:], strict=False))


def test_a_line_is_kept_whole_when_it_fits() -> None:
    """A line boundary is preferred to a sentence or word boundary."""
    lines = [f"Line {n} holds a complete thought of about thirty chars." for n in range(1, 9)]
    text = "\n".join(lines)

    chunks = chunk_text(text, chunk_size=130, chunk_overlap=0)

    for chunk in chunks:
        assert set(chunk.split("\n")) <= set(lines), chunk


def test_a_long_line_is_split_at_sentence_ends_before_word_ends() -> None:
    sentences = [f"This is sentence number {n} of the same line." for n in range(1, 11)]
    text = " ".join(sentences)

    chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)

    for chunk in chunks:
        assert chunk.endswith("line."), chunk


def test_a_single_word_longer_than_the_chunk_size_is_still_split() -> None:
    """The one unavoidable mid-word cut: there is no boundary to use."""
    url = "https://example.com/" + "a" * 60

    chunks = chunk_text(f"See {url} now", chunk_size=30, chunk_overlap=5)

    assert "".join(chunks).count("a") >= 60
    assert max(len(chunk) for chunk in chunks) <= 30


# --- minimum chunk size --------------------------------------------------------


def test_tiny_paragraphs_are_merged_into_the_next_chunk() -> None:
    text = "Sam Sample\n\nWestfield\n\nhttps://example.com/sam\n\n" + "A real paragraph. " * 12

    chunks = chunk_text(text.strip(), chunk_size=400, chunk_overlap=50, min_size=60)

    assert len(chunks) == 1
    assert chunks[0].startswith("Sam Sample")
    assert "Westfield" in chunks[0]
    assert "A real paragraph." in chunks[0]


def test_a_tiny_last_chunk_is_merged_into_the_previous_one() -> None:
    text = "A real paragraph. " * 10 + "\n\nThe end."

    chunks = chunk_text(text, chunk_size=400, chunk_overlap=50, min_size=30)

    assert len(chunks) == 1
    assert chunks[0].endswith("The end.")


def test_paragraphs_at_least_min_size_are_left_alone() -> None:
    text = "First paragraph long enough.\n\nSecond paragraph long enough."

    assert chunk_text(text, chunk_size=200, chunk_overlap=20, min_size=20) == [
        "First paragraph long enough.",
        "Second paragraph long enough.",
    ]


def test_a_document_that_is_only_one_tiny_chunk_stays_a_chunk() -> None:
    assert chunk_text("Some fact.", chunk_size=200, chunk_overlap=20, min_size=100) == [
        "Some fact."
    ]


def test_min_size_zero_changes_nothing() -> None:
    text = "Para one.\n\nPara two.\n\nPara three."

    assert chunk_text(text, chunk_size=100, chunk_overlap=10, min_size=0) == [
        "Para one.",
        "Para two.",
        "Para three.",
    ]


def test_a_merged_chunk_exceeds_chunk_size_by_less_than_min_size() -> None:
    full = " ".join(["word"] * 20)  # 99 characters, so "tiny" + 2 + 99 is more than 100
    text = f"{full}\n\ntiny\n\n{full}"

    chunks = chunk_text(text, chunk_size=100, chunk_overlap=0, min_size=20)

    assert all(len(chunk) < 100 + 20 for chunk in chunks)
    assert any("tiny" in chunk and len(chunk) > 100 for chunk in chunks)


def test_chunk_min_size_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="min_size"):
        chunk_text("some text", chunk_size=10, chunk_overlap=2, min_size=10)
