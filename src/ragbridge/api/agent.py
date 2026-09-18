"""POST /agent: answer a question with a bounded, multi-step search."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge import tracing
from ragbridge.agent.loop import run_agent
from ragbridge.agent.planner import Planner, get_planner
from ragbridge.api.query import SNIPPET_LENGTH, Source
from ragbridge.auth import get_tenant
from ragbridge.chat import Chatter, get_chatter
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Tenant
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.rerank import Reranker, get_reranker
from ragbridge.retrieval import SearchResult, hybrid_search

router = APIRouter(tags=["agent"])

TOP_K_PER_STEP = 5
"""Chunks kept from each search, after reranking."""


class AgentRequest(BaseModel):
    question: str
    max_steps: int | None = Field(default=None, ge=1)
    """Ask for fewer searches than the server allows. Never more: the
    ceiling is settings.agent_max_steps (decision 4, docs/plans/phase-4.md).
    """


class StepInfo(BaseModel):
    query: str
    results: int


class AgentResponse(BaseModel):
    answer: str
    sources: list[Source]
    steps: list[StepInfo]
    step_count: int


@router.post("/agent", response_model=AgentResponse)
async def answer_with_agent(
    request: AgentRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chatter: Annotated[Chatter, Depends(get_chatter)],
    reranker: Annotated[Reranker, Depends(get_reranker)],
    planner: Annotated[Planner, Depends(get_planner)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AgentResponse:
    """Search up to ``max_steps`` times, then answer from everything found.

    Every search is tenant-scoped exactly like ``POST /query``: the same
    ``hybrid_search`` with the authenticated tenant's id. The response
    lists the steps taken so a wrong answer can be traced back to what
    was searched (decision 6, docs/plans/phase-4.md). Synchronous on
    purpose - the step ceiling already bounds the run.
    """
    max_steps = min(request.max_steps or settings.agent_max_steps, settings.agent_max_steps)

    async def search(query: str) -> list[SearchResult]:
        [embedding] = await embedder.embed([query])
        candidates = await hybrid_search(
            session,
            embedding,
            query,
            mode=settings.retrieval_mode,
            candidates=settings.retrieval_candidates,
            tenant_id=tenant.id,
        )
        return await reranker.rerank(query, candidates, TOP_K_PER_STEP)

    with tracing.span(settings, "agent", question=request.question, max_steps=max_steps) as span:
        run = await run_agent(request.question, planner=planner, search=search, max_steps=max_steps)
        answer = await chatter.answer(request.question, [chunk.content for chunk, _, _ in run.rows])
        response = AgentResponse(
            answer=answer,
            sources=[
                Source(
                    document_id=document.id,
                    filename=document.filename,
                    chunk_index=chunk.chunk_index,
                    snippet=chunk.content[:SNIPPET_LENGTH],
                    score=score,
                )
                for chunk, document, score in run.rows
            ],
            steps=[StepInfo(query=step.query, results=step.results) for step in run.steps],
            step_count=len(run.steps),
        )
        span.update(output=response.model_dump())

    return response
