"""POST /query: answer a question using retrieval-augmented generation."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.chat import Chatter, get_chatter
from ragbridge.db.models import Chunk, Document
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder

router = APIRouter(tags=["query"])

SNIPPET_LENGTH = 300


class QueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1, le=20)


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
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chatter: Annotated[Chatter, Depends(get_chatter)],
) -> QueryResponse:
    """Embed the question, retrieve the nearest chunks, and answer from them."""
    [question_embedding] = await embedder.embed([request.question])

    distance = Chunk.embedding.cosine_distance(question_embedding).label("distance")
    result = await session.execute(
        select(Chunk, Document, distance)
        .join(Document, Chunk.document_id == Document.id)
        .order_by(distance)
        .limit(request.top_k)
    )
    rows = result.all()

    answer = await chatter.answer(request.question, [chunk.content for chunk, _, _ in rows])
    sources = [
        Source(
            document_id=document.id,
            filename=document.filename,
            chunk_index=chunk.chunk_index,
            snippet=chunk.content[:SNIPPET_LENGTH],
            score=1 - cosine_distance,
        )
        for chunk, document, cosine_distance in rows
    ]

    return QueryResponse(answer=answer, sources=sources)
