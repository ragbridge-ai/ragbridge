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
