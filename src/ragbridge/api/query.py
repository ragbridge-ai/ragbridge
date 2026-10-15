"""POST /query: answer a question using retrieval-augmented generation."""

import hashlib
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge import tracing
from ragbridge.access import AccessGroups, normalise_scope
from ragbridge.api.explain import RetrievalInfo, build_retrieval_info
from ragbridge.auth import Caller, check_access_scope, get_caller
from ragbridge.cache import Cache, get_cache
from ragbridge.chat import Chatter, get_chatter
from ragbridge.config import Settings, get_settings
from ragbridge.context import add_neighbours, build_context
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.rerank import Reranker, get_reranker
from ragbridge.retrieval import fetch_neighbours, hybrid_search_with_provenance

router = APIRouter(tags=["query"])

SNIPPET_LENGTH = 300


class QueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] | None = None
    """Which retrieval arm(s) to use. Defaults to settings.retrieval_mode."""
    explain: bool = False
    """Add a ``retrieval`` object to each source: which arm found it, and where."""
    access_groups: AccessGroups | None = None
    """Only use chunks of documents sharing at least one of these groups, for retrieval and
    for the surrounding context. Missing: use everything. An empty list: a caller with no
    groups, who gets no sources."""


class Source(BaseModel):
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    snippet: str
    score: float
    context_only: bool = False
    """``True`` for a chunk the model was given as surrounding context that retrieval did
    not return: it was not scored (``score`` is 0.0) and does not count towards ``top_k``.
    """


class QueryResponse(BaseModel):
    """The plain response, used by MCP and ``RagbridgeClient``.

    The endpoint itself answers with ``ExplainableQueryResponse``, so the
    MCP ``ask`` tool's output schema does not change when ``explain`` is
    added - the same split as ``ragbridge.api.search``.
    """

    answer: str
    sources: list[Source]


class ExplainableSource(Source):
    retrieval: RetrievalInfo | None = None
    """Only present when the request asked for ``explain``."""


class ExplainableQueryResponse(BaseModel):
    answer: str
    sources: list[ExplainableSource]


def _settings_fingerprint(settings: Settings) -> str:
    """A short hash of every setting, so a cached answer is only served under the settings that
    produced it.

    The corpus version says the *documents* are unchanged; it says nothing about the chat model,
    the temperature, retrieval, reranking or chunk-neighbour settings, all of which change the
    answer. Without this, an operator who changes one and restarts keeps getting the old
    answers until the cache entries expire, and a fix appears not to work. Hashing all of the
    settings (rather than a list of the ones that matter) means a setting added later cannot be
    forgotten; the price is that changing any setting, even one that does not shape answers,
    starts the cache afresh, which costs a few recomputed answers. Only the hash is stored.
    """
    return hashlib.sha256(settings.model_dump_json().encode()).hexdigest()[:16]


def _scope_key(access_groups: list[str] | None) -> str:
    """The access scope as it enters the answer-cache key.

    Without it, an answer built from one group's documents could be served to a caller in
    another. ``*`` is no scope at all and an empty string is a scope of no groups: a group
    name cannot be either, so no scope can be mistaken for another. The groups arrive
    sorted (``normalise_scope``), so the same set in another order shares an entry.
    """
    return "*" if access_groups is None else ",".join(access_groups)


@router.post("/query", response_model=ExplainableQueryResponse, response_model_exclude_unset=True)
async def answer_query(
    request: QueryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    caller: Annotated[Caller, Depends(get_caller)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chatter: Annotated[Chatter, Depends(get_chatter)],
    reranker: Annotated[Reranker, Depends(get_reranker)],
    settings: Annotated[Settings, Depends(get_settings)],
    cache: Annotated[Cache, Depends(get_cache)],
) -> ExplainableQueryResponse:
    """Embed the question, retrieve and rerank chunks, and answer from them.

    When ``settings.answer_cache_enabled``, the whole response is cached
    under a key that includes the tenant's current corpus version and a
    fingerprint of the settings (``_settings_fingerprint``) - a
    number that ``ingest_document`` and document deletion both bump.
    Bumping it invalidates every cached answer for that tenant at once,
    without enumerating or deleting a single key: the old keys simply
    become unreachable and expire on their own TTL (decision 7,
    docs/plans/phase-3.md). A cache hit returns before the span below
    even opens - nothing was computed, so there is nothing to trace.

    The embedding, retrieval, reranking, and generation steps run
    inside one ``tracing.span``, so LiteLLM's own per-call spans (the
    embedding call, the chat completion) nest under it instead of each
    appearing as an unrelated top-level trace - retrieval itself is not
    an LLM call and would otherwise never appear in a trace at all
    (decision 8, docs/plans/phase-3.md).

    The model is given the retrieved chunks and their neighbours as continuous,
    labelled excerpts in document order, with gap notes (``build_context``), so a
    bullet is read under its own heading. ``sources`` lists exactly the chunks the
    model received, one entry per chunk: the ``top_k`` retrieved ones first, in
    score order, then the neighbours, each marked ``context_only`` with score 0.0.

    ``explain`` is part of the cache key: an answer cached without it
    must not be served to a request that asked for the retrieval detail,
    nor the reverse. With ``explain`` each source also says which arm
    found it (see ``ragbridge.api.search`` for why those fields are unset,
    not ``None``, otherwise).
    """
    check_access_scope(caller, request.access_groups)
    tenant = caller.tenant
    access_groups = normalise_scope(request.access_groups)
    cache_key: str | None = None
    if settings.answer_cache_enabled:
        corpus_version = await cache.get(f"corpus_version:{tenant.id}") or "0"
        request_digest = hashlib.sha256(
            f"{_settings_fingerprint(settings)}|{request.question}|{request.mode}|"
            f"{request.top_k}|{request.explain}|{_scope_key(access_groups)}".encode()
        ).hexdigest()
        cache_key = f"answer:{tenant.id}:{corpus_version}:{request_digest}"
        cached = await cache.get(cache_key)
        if cached is not None:
            return ExplainableQueryResponse.model_validate_json(cached)

    with tracing.span(
        settings, "query", question=request.question, mode=request.mode, top_k=request.top_k
    ) as span:
        [question_embedding] = await embedder.embed([request.question])

        candidates, provenance = await hybrid_search_with_provenance(
            session,
            question_embedding,
            request.question,
            mode=request.mode or settings.retrieval_mode,
            candidates=settings.retrieval_candidates,
            max_term_share=settings.keyword_max_term_frequency,
            rarity_weighting=settings.keyword_rarity_weighting,
            tenant_id=tenant.id,
            access_groups=access_groups,
        )
        rows = await reranker.rerank(request.question, candidates, request.top_k)

        shown = rows
        if settings.answer_context_neighbours > 0:
            neighbours = await fetch_neighbours(
                session,
                rows,
                distance=settings.answer_context_neighbours,
                tenant_id=tenant.id,
                access_groups=access_groups,
            )
            shown = add_neighbours(
                rows,
                neighbours,
                distance=settings.answer_context_neighbours,
                max_chars=settings.answer_context_max_chars,
            )
        answer = await chatter.answer(request.question, build_context(shown))
        sources = [
            ExplainableSource(
                document_id=document.id,
                filename=document.filename,
                chunk_index=chunk.chunk_index,
                snippet=chunk.content[:SNIPPET_LENGTH],
                score=score,
                context_only=position >= len(rows),
            )
            for position, (chunk, document, score) in enumerate(shown)
        ]
        if request.explain:
            for source, (chunk, _, _) in zip(sources, rows, strict=False):
                source.retrieval = build_retrieval_info(candidates, provenance, chunk.id)
        response = ExplainableQueryResponse(answer=answer, sources=sources)
        span.update(output=response.model_dump(exclude_unset=True))

    if cache_key is not None:
        await cache.set(
            cache_key, response.model_dump_json(exclude_unset=True), ttl=settings.answer_cache_ttl
        )

    return response
