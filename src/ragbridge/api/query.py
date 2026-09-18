"""POST /query: answer a question using retrieval-augmented generation."""

import hashlib
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge import tracing
from ragbridge.auth import get_tenant
from ragbridge.cache import Cache, get_cache
from ragbridge.chat import Chatter, get_chatter
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Tenant
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.rerank import Reranker, get_reranker
from ragbridge.retrieval import hybrid_search

router = APIRouter(tags=["query"])

SNIPPET_LENGTH = 300


class QueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] | None = None
    """Which retrieval arm(s) to use. Defaults to settings.retrieval_mode."""


class Source(BaseModel):
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    snippet: str
    score: float


class QueryResponse(BaseModel):
    answer: str
    sources: list[Source]


@router.post("/query", response_model=QueryResponse)
async def answer_query(
    request: QueryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chatter: Annotated[Chatter, Depends(get_chatter)],
    reranker: Annotated[Reranker, Depends(get_reranker)],
    settings: Annotated[Settings, Depends(get_settings)],
    cache: Annotated[Cache, Depends(get_cache)],
) -> QueryResponse:
    """Embed the question, retrieve and rerank chunks, and answer from them.

    When ``settings.answer_cache_enabled``, the whole response is cached
    under a key that includes the tenant's current corpus version - a
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
    """
    cache_key: str | None = None
    if settings.answer_cache_enabled:
        corpus_version = await cache.get(f"corpus_version:{tenant.id}") or "0"
        request_digest = hashlib.sha256(
            f"{request.question}|{request.mode}|{request.top_k}".encode()
        ).hexdigest()
        cache_key = f"answer:{tenant.id}:{corpus_version}:{request_digest}"
        cached = await cache.get(cache_key)
        if cached is not None:
            return QueryResponse.model_validate_json(cached)

    with tracing.span(
        settings, "query", question=request.question, mode=request.mode, top_k=request.top_k
    ) as span:
        [question_embedding] = await embedder.embed([request.question])

        candidates = await hybrid_search(
            session,
            question_embedding,
            request.question,
            mode=request.mode or settings.retrieval_mode,
            candidates=settings.retrieval_candidates,
            tenant_id=tenant.id,
        )
        rows = await reranker.rerank(request.question, candidates, request.top_k)

        answer = await chatter.answer(request.question, [chunk.content for chunk, _, _ in rows])
        sources = [
            Source(
                document_id=document.id,
                filename=document.filename,
                chunk_index=chunk.chunk_index,
                snippet=chunk.content[:SNIPPET_LENGTH],
                score=score,
            )
            for chunk, document, score in rows
        ]
        response = QueryResponse(answer=answer, sources=sources)
        span.update(output=response.model_dump())

    if cache_key is not None:
        await cache.set(cache_key, response.model_dump_json(), ttl=settings.answer_cache_ttl)

    return response
