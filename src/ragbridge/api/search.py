"""POST /search: retrieve the best-matching chunks, without generating an answer."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.api.explain import RetrievalInfo, build_retrieval_info
from ragbridge.auth import get_tenant
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Tenant
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.rerank import Reranker, get_reranker
from ragbridge.retrieval import hybrid_search_with_provenance

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] | None = None
    """Which retrieval arm(s) to use. Defaults to settings.retrieval_mode."""
    explain: bool = False
    """Add a ``retrieval`` object to each hit, and ``candidate_count`` to the response."""


class _HitFields(BaseModel):
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    """The whole chunk, not a snippet: the caller's own model reads it."""
    score: float


class SearchHit(_HitFields):
    """A hit as MCP clients and ``RagbridgeClient`` see it: no retrieval detail."""


class SearchResponse(BaseModel):
    """The plain response, used by MCP and ``RagbridgeClient``.

    The endpoint itself answers with ``ExplainableSearchResponse``, so the
    MCP tool's output schema does not change when ``explain`` is added
    (a shared model would have grown two null fields there).
    """

    results: list[SearchHit]


class ExplainableSearchHit(_HitFields):
    retrieval: RetrievalInfo | None = None
    """Only present when the request asked for ``explain``."""


class ExplainableSearchResponse(BaseModel):
    results: list[ExplainableSearchHit]
    candidate_count: int | None = None
    """How many chunks retrieval found before the reranker narrowed them to
    ``top_k``. Only present when the request asked for ``explain``.
    """


@router.post("/search", response_model=ExplainableSearchResponse, response_model_exclude_unset=True)
async def search_documents(
    request: SearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    reranker: Annotated[Reranker, Depends(get_reranker)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExplainableSearchResponse:
    """Embed the query, retrieve and rerank the tenant's chunks, and return them.

    The same retrieval ``POST /query`` runs, stopping before generation.
    It exists for callers that do their own reasoning over the chunks,
    such as an MCP client (decision 9, docs/plans/phase-4.md).

    With ``explain`` each hit also says which retrieval arm found it and
    where it stood before reranking. Those fields are only *set* when
    ``explain`` is true, and ``response_model_exclude_unset`` leaves unset
    fields out of the JSON - so a request without ``explain`` gets exactly
    the response it always did. (``exclude_none`` would be wrong here: a
    ``keyword_rank`` of ``null`` means "that arm did not find this chunk",
    which is information.)
    """
    [embedding] = await embedder.embed([request.query])
    candidates, provenance = await hybrid_search_with_provenance(
        session,
        embedding,
        request.query,
        mode=request.mode or settings.retrieval_mode,
        candidates=settings.retrieval_candidates,
        max_term_share=settings.keyword_max_term_frequency,
        tenant_id=tenant.id,
    )
    rows = await reranker.rerank(request.query, candidates, request.top_k)
    hits = [
        ExplainableSearchHit(
            document_id=document.id,
            filename=document.filename,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            score=score,
        )
        for chunk, document, score in rows
    ]
    response = ExplainableSearchResponse(results=hits)
    if request.explain:
        for hit, (chunk, _, _) in zip(hits, rows, strict=True):
            hit.retrieval = build_retrieval_info(candidates, provenance, chunk.id)
        response.candidate_count = len(candidates)
    return response
