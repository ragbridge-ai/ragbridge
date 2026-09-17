"""Unit tests for the fake embedder. No network calls, no database needed."""

import asyncio

from ragbridge.embeddings import FakeEmbedder


def test_fake_embedder_returns_one_vector_per_text() -> None:
    embedder = FakeEmbedder(dimension=8)

    result = asyncio.run(embedder.embed(["hello", "world"]))

    assert len(result) == 2
    assert all(len(vector) == 8 for vector in result)


def test_fake_embedder_is_deterministic_per_text() -> None:
    embedder = FakeEmbedder(dimension=8)

    first = asyncio.run(embedder.embed(["hello, ragbridge"]))
    second = asyncio.run(embedder.embed(["hello, ragbridge"]))

    assert first == second


def test_fake_embedder_gives_different_texts_different_vectors() -> None:
    embedder = FakeEmbedder(dimension=8)

    result = asyncio.run(embedder.embed(["hello", "goodbye"]))

    assert result[0] != result[1]
