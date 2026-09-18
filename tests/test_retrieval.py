"""Tests for the vector and keyword search functions."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.config import get_settings
from ragbridge.embeddings import FakeEmbedder
from ragbridge.retrieval import keyword_search, vector_search


def test_keyword_search_finds_a_literal_token_that_vector_search_misses(
    app_with_database: FastAPI,
) -> None:
    """The gap the second retrieval arm exists to close.

    ``FakeEmbedder`` has no semantics - it turns each distinct text into an
    unrelated random vector - so nothing here claims vector search is
    "bad". It shows the specific failure mode hybrid search targets: a
    rare, literal token (an error code) that keyword search matches
    exactly and that a semantics-free embedder has no way to connect to
    the question.
    """
    client = TestClient(app_with_database)
    error_chunk = "Error code ERR_4021 means the upload exceeded the size limit."
    other_chunk = "Cats are independent and curious animals."
    client.post("/documents", files={"file": ("errors.txt", error_chunk.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", other_chunk.encode(), "text/plain")})

    async def run_both_arms() -> tuple[list[str], list[str]]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        dimension = get_settings().embedding_dimension
        [embedding] = await FakeEmbedder(dimension).embed(["ERR_4021"])
        async with session_factory() as session:
            vector_rows = await vector_search(session, embedding, limit=5)
            keyword_rows = await keyword_search(session, "ERR_4021", limit=5)
        return (
            [chunk.content for chunk, _, _ in vector_rows],
            [chunk.content for chunk, _, _ in keyword_rows],
        )

    vector_order, keyword_order = asyncio.run(run_both_arms())

    assert keyword_order[0] == error_chunk
    assert vector_order[0] != error_chunk


def test_keyword_search_excludes_chunks_with_no_matching_terms(
    app_with_database: FastAPI,
) -> None:
    """``websearch_to_tsquery`` filters with ``@@``, it does not just rank.

    A chunk sharing no terms with the query must not appear at all, unlike
    vector search where every row gets some (possibly meaningless) score.
    """
    client = TestClient(app_with_database)
    client.post(
        "/documents",
        files={"file": ("a.txt", b"Cats are independent and curious animals.", "text/plain")},
    )

    async def run_keyword_search() -> list[str]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        async with session_factory() as session:
            rows = await keyword_search(session, "ERR_4021", limit=5)
        return [chunk.content for chunk, _, _ in rows]

    results = asyncio.run(run_keyword_search())

    assert results == []
