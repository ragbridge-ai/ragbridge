"""The ``retrieval`` object that ``explain: true`` adds to search results.

Shared by ``POST /search`` and ``POST /query`` so both describe a hit the
same way (decision 8, docs/plans/phase-6-ui.md).
"""

import uuid

from pydantic import BaseModel

from ragbridge.retrieval import ChunkProvenance, SearchResult


class RetrievalInfo(BaseModel):
    """Why a chunk is in the results: which arm found it, and what reranking did."""

    vector_rank: int | None
    """1-based rank in the vector arm; ``None`` if that arm did not find the chunk."""
    keyword_rank: int | None
    """1-based rank in the keyword arm; ``None`` if that arm did not find the chunk."""
    fused_score: float
    """The score before reranking: an RRF score in "hybrid" mode, else the arm's own score."""
    rank_before_rerank: int
    """1-based position among the candidates before the reranker ran.

    Equal to the hit's final position when reranking is off (the default
    reranker only truncates), which is itself the sign that it is off.
    """


def build_retrieval_info(
    candidates: list[SearchResult],
    provenance: dict[uuid.UUID, ChunkProvenance],
    chunk_id: uuid.UUID,
) -> RetrievalInfo:
    """Describe one returned chunk, given the candidate list the reranker was handed."""
    position = next(
        rank for rank, (chunk, _, _) in enumerate(candidates, start=1) if chunk.id == chunk_id
    )
    found = provenance[chunk_id]
    return RetrievalInfo(
        vector_rank=found.vector_rank,
        keyword_rank=found.keyword_rank,
        fused_score=found.fused_score,
        rank_before_rerank=position,
    )
