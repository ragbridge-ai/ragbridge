"""POST /search: retrieve the best-matching chunks, without generating an answer."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.auth import get_tenant
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Tenant
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.rerank import Reranker, get_reranker
from ragbridge.retrieval import hybrid_search

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] | None = None
    """Which retrieval arm(s) to use. Defaults to settings.retrieval_mode."""


class SearchHit(BaseModel):
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    """The whole chunk, not a snippet: the caller's own model reads it."""
    score: float


class SearchResponse(BaseModel):
    results: list[SearchHit]


@router.post("/search", response_model=SearchResponse)
async def search_documents(
    request: SearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    reranker: Annotated[Reranker, Depends(get_reranker)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchResponse:
    """Embed the query, retrieve and rerank the tenant's chunks, and return them.

    The same retrieval ``POST /query`` runs, stopping before generation.
    It exists for callers that do their own reasoning over the chunks,
    such as an MCP client (decision 9, docs/plans/phase-4.md).
    """
    [embedding] = await embedder.embed([request.query])
    candidates = await hybrid_search(
        session,
        embedding,
        request.query,
        mode=request.mode or settings.retrieval_mode,
        candidates=settings.retrieval_candidates,
        tenant_id=tenant.id,
    )
    rows = await reranker.rerank(request.query, candidates, request.top_k)
    return SearchResponse(
        results=[
            SearchHit(
                document_id=document.id,
                filename=document.filename,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                score=score,
            )
            for chunk, document, score in rows
        ]
    )
