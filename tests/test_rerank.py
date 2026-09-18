"""Tests for NoOpReranker and FakeReranker.

Both are pure - no session, no network call - so they are unit tested
directly, in memory, the same way reciprocal_rank_fusion is.
LiteLLMReranker is exercised only through real usage (Phase 1 decision 5:
tests and CI never call a real provider).
"""

import asyncio
import uuid

from ragbridge.db.models import Chunk, Document
from ragbridge.rerank import FakeReranker, NoOpReranker
from ragbridge.retrieval import SearchResult


def _make_chunk() -> Chunk:
    return Chunk(id=uuid.uuid4(), document_id=uuid.uuid4(), chunk_index=0, content="", metadata_={})


def _make_document() -> Document:
    return Document(
        id=uuid.uuid4(), filename="a.txt", content_type="text/plain", sha256="x", content="x"
    )


def test_no_op_reranker_keeps_order_and_truncates_to_top_k() -> None:
    document = _make_document()
    candidates: list[SearchResult] = [(_make_chunk(), document, score) for score in (0.9, 0.5, 0.1)]

    result = asyncio.run(NoOpReranker().rerank("question", candidates, top_k=2))

    assert result == candidates[:2]


def test_no_op_reranker_of_no_candidates_returns_empty_list() -> None:
    result = asyncio.run(NoOpReranker().rerank("question", [], top_k=5))

    assert result == []


def test_fake_reranker_reverses_order_and_truncates_to_top_k() -> None:
    document = _make_document()
    candidates: list[SearchResult] = [(_make_chunk(), document, score) for score in (0.9, 0.5, 0.1)]

    result = asyncio.run(FakeReranker().rerank("question", candidates, top_k=2))

    assert result == list(reversed(candidates))[:2]
