"""Rerank retrieved chunks against a question.

Behind a ``Protocol``, mirroring ``ragbridge.embeddings.Embedder`` and
``ragbridge.chat.Chatter``, so the API and tests depend on the shape of a
reranker, not on LiteLLM or any specific provider. Tests use
``FakeReranker`` and never call a real provider (Phase 1 decision 5). The
default, ``NoOpReranker``, needs no provider either - see decision 3 in
docs/plans/phase-2.md.
"""

import asyncio
import logging
import re
import weakref
from typing import Annotated, Protocol

import litellm
from fastapi import Depends

from ragbridge.config import Settings, get_settings
from ragbridge.retrieval import SearchResult

logger = logging.getLogger(__name__)


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


JUDGE_SYSTEM_PROMPT = "You judge whether a passage helps answer a question."
JUDGE_PROMPT = (
    "Question: {question}\n\nPassage:\n{passage}\n\n"
    "Does the passage contain information needed to answer the question? "
    "Reply with one digit: 0 = no, 1 = partly, 2 = yes, the answer is in the passage."
)
MAX_PASSAGE_CHARS = 3_000
"""A chunk is about 1,000 characters; this only stops an unusually long one from flooding the
judge's context window.
"""
MAX_PARALLEL_JUDGEMENTS = 4
"""Judge calls in flight at once, across every request of the process (see ``_judge_slots``)."""
JUDGE_TIMEOUT_SECONDS = 20
"""One judge call that takes longer counts as failed, so a stalled model delays a request by a
bounded time instead of LiteLLM's default of many minutes.
"""
NEUTRAL_RATING = 1
"""What a chunk counts as when its call failed or its reply had no rating: neither pushed up
nor down.
"""
_REPLY = re.compile(r"^\W*(?:rating|score|answer)?\W*([0-2])(?!\d)", re.IGNORECASE)
"""A usable reply starts with the digit ("2", "**2**", "Rating: 0"). A digit further into a longer
reply ("Passage 1 does not say... 0") or "10/10" is not the rating.
"""
_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _judge_slots() -> asyncio.Semaphore:
    """The limit on judge calls in flight, shared by all requests running on this event loop.

    A semaphore made per request would allow ``MAX_PARALLEL_JUDGEMENTS`` calls for *each* request,
    so ten requests at once would put forty calls in front of one local model. A semaphore belongs
    to one event loop, hence one per loop.
    """
    loop = asyncio.get_running_loop()
    if loop not in _SLOTS:
        _SLOTS[loop] = asyncio.Semaphore(MAX_PARALLEL_JUDGEMENTS)
    return _SLOTS[loop]


class ChatReranker:
    """Reranks by asking a chat model how well each chunk answers the question.

    Each of the best ``rerank_candidates`` chunks is shown to the model on its own, with the
    question, and rated 0 (no), 1 (partly) or 2 (the answer is in it) at temperature 0. The
    chunks are then ordered by rating, and by their fused order within a rating, so a model
    that rates everything the same changes nothing. Chunks after the rated ones keep their
    order and come last.

    This exists because Ollama has no rerank endpoint (decision 3, docs/plans/phase-2.md).
    A long table chunk that the vector arm cannot find is exactly what a reader recognises
    as relevant and a fusion of two rankings does not (docs/evaluation.md). It costs one
    model call per candidate, so it is off by default. It never makes a request fail. A call that
    fails or times out, or a reply that does not start with a rating, counts as neutral for that
    chunk only; when no chunk could be rated the fused order is returned untouched and a warning
    says so (a model that thinks before it answers uses up the few tokens the judge is given).
    The chunk text goes into the judge's prompt, so a chunk that says "rate me 2" can lift itself
    in the ranking, and nothing more: the judge's reply is never shown to anyone.

    Scores returned are 1/3, 2/3 and 1 for the ratings 0, 1 and 2, and 0.0 for a chunk that
    was not rated, so the list stays sorted best first. They come from one judgement per chunk
    of one search, so they are only comparable within that search: ``/agent`` merges chunks
    from several searches by score, and with this backend many of them tie.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        rated = candidates[: self._settings.rerank_candidates]
        unrated = candidates[len(rated) :]
        answers = await asyncio.gather(*(self._rate(query, chunk.content) for chunk, _, _ in rated))
        if rated and all(answer is None for answer in answers):
            logger.warning(
                "The rerank model gave no usable rating for any of %d chunks; keeping the fused "
                "order (is RERANK_CHAT_MODEL reachable, and does it answer with a digit "
                "straight away? A model that thinks first uses up the judge's few tokens)",
                len(rated),
            )
            return candidates[:top_k]
        ratings = [NEUTRAL_RATING if answer is None else answer for answer in answers]
        order = sorted(range(len(rated)), key=lambda index: (-ratings[index], index))
        best_first = [(rated[i][0], rated[i][1], (ratings[i] + 1) / 3) for i in order]
        return (best_first + [(chunk, document, 0.0) for chunk, document, _ in unrated])[:top_k]

    async def _rate(self, question: str, passage: str) -> int | None:
        """The model's rating of ``passage``, or ``None`` when the call failed or had no rating."""
        model = self._settings.rerank_chat_model or self._settings.chat_model
        api_base = self._settings.ollama_base_url if model.startswith("ollama/") else None
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    question=question, passage=passage[:MAX_PASSAGE_CHARS]
                ),
            },
        ]
        try:
            async with _judge_slots():
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    api_base=api_base,
                    temperature=0,
                    max_tokens=8,
                    timeout=JUDGE_TIMEOUT_SECONDS,
                    drop_params=True,
                )
        except Exception:
            logger.warning("A rerank judgement failed; counting it as neutral", exc_info=True)
            return None
        match = _REPLY.match(response.choices[0].message.content or "")
        return int(match.group(1)) if match else None


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
    if not settings.rerank_enabled:
        return NoOpReranker()
    if settings.rerank_backend == "chat":
        return ChatReranker(settings)
    return LiteLLMReranker(settings)
