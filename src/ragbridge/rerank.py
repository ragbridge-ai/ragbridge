"""Rerank retrieved chunks against a question.

Behind a ``Protocol``, mirroring ``ragbridge.embeddings.Embedder`` and
``ragbridge.chat.Chatter``, so the API and tests depend on the shape of a
reranker, not on LiteLLM or any specific provider. Tests use
``FakeReranker`` and never call a real provider (Phase 1 decision 5). The
default, ``NoOpReranker``, needs no provider either - see decision 3 in
docs/plans/phase-2.md.
"""

from typing import Annotated, Protocol

import litellm
from fastapi import Depends

from ragbridge.config import Settings, get_settings
from ragbridge.retrieval import SearchResult


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        """Return the best top_k of candidates for query, best first."""
        ...


class NoOpReranker:
    """Keeps candidates in the given order, only truncating to top_k.

    The default (decision 3, docs/plans/phase-2.md): Ollama has no rerank
    endpoint, and a local cross-encoder means bundling torch (~2 GB) for a
    feature most installs will leave off. A fresh ``docker compose up``
    reranks nothing until ``RERANK_ENABLED`` is switched on.
    """

    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        return candidates[:top_k]


class LiteLLMReranker:
    """Reranks candidates through LiteLLM's rerank API.

    Works with any provider LiteLLM's rerank call supports (Cohere,
    Voyage, Jina) - LiteLLM standardises the response shape across them,
    the same way it already does for chat and embeddings.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        if not candidates:
            return []

        documents = [chunk.content for chunk, _, _ in candidates]
        response = await litellm.arerank(
            model=self._settings.rerank_model, query=query, documents=documents, top_n=top_k
        )
        return [
            (
                candidates[result["index"]][0],
                candidates[result["index"]][1],
                result["relevance_score"],
            )
            for result in response.results
        ]


class FakeReranker:
    """Deterministic reranker for tests: never makes a network call.

    Reverses the given order before truncating, so a test can assert that
    reranking actually changed something, not merely that it ran.
    """

    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        return list(reversed(candidates))[:top_k]


def get_reranker(settings: Annotated[Settings, Depends(get_settings)]) -> Reranker:
    """FastAPI dependency returning the configured reranker.

    Tests override this with a ``FakeReranker`` via
    ``app.dependency_overrides``, the same way ``get_embedder`` and
    ``get_chatter`` are overridden.
    """
    if settings.rerank_enabled:
        return LiteLLMReranker(settings)
    return NoOpReranker()
