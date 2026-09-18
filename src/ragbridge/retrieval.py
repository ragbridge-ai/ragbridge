"""Retrieval functions: turn a query into ranked chunks.

Two independent arms, kept as separate functions so each can be tested,
composed, and swapped on its own. Phase 2 step 2 fuses their results with
Reciprocal Rank Fusion; this step only gives each arm a home and a shared
return shape, so fusion has something uniform to work with.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.db.models import Chunk, Document

SearchResult = tuple[Chunk, Document, float]
"""One retrieved chunk, its document, and a relevance score.

Higher is always better, for both arms - callers never need to know which
arm a score came from to compare it, even though the two arms compute a
score on entirely different scales (cosine similarity vs. ``ts_rank``).
"""


async def vector_search(
    session: AsyncSession, embedding: list[float], limit: int
) -> list[SearchResult]:
    """Return the chunks whose embedding is nearest to ``embedding``, nearest first.

    Score is ``1 - cosine_distance``: 1.0 for an exact match, lower for a
    less similar chunk - the same convention ``POST /query`` used in
    Phase 1, kept here so this is a pure refactor of that endpoint.
    """
    distance = Chunk.embedding.cosine_distance(embedding).label("distance")
    result = await session.execute(
        select(Chunk, Document, distance)
        .join(Document, Chunk.document_id == Document.id)
        .order_by(distance)
        .limit(limit)
    )
    return [(chunk, document, 1 - distance) for chunk, document, distance in result.all()]


async def keyword_search(session: AsyncSession, query: str, limit: int) -> list[SearchResult]:
    """Return the chunks that best match ``query`` by full-text search, best first.

    Uses ``websearch_to_tsquery``, the parser built for text a user actually
    types (bare words, ``"quoted phrases"``, ``or``, ``-excluded``) - unlike
    ``to_tsquery`` it never raises on a plain sentence. Score is
    ``ts_rank``, PostgreSQL's own relevance measure for a tsquery match
    against a tsvector.
    """
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(Chunk.content_tsv, tsquery).label("rank")
    result = await session.execute(
        select(Chunk, Document, rank)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.content_tsv.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    return [(chunk, document, rank) for chunk, document, rank in result.all()]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchResult]], *, k: int = 60
) -> list[SearchResult]:
    """Merge several rankings of the same items with Reciprocal Rank Fusion.

    Each ranking is one retrieval arm's results, best first. An item is
    identified by its chunk id; its fused score is the sum, over every
    ranking it appears in, of ``1 / (k + rank)`` (rank is 1-based). This
    uses only an item's *position* in each ranking, never the arm's own
    score - a cosine similarity and a ``ts_rank`` are not on a comparable
    scale, so combining them by position needs no tuning and no score
    normalisation (see docs/plans/phase-2.md, decision 1).

    A pure function: no session, no ``await``. Returns one row per
    distinct chunk, sorted by fused score, best first.
    """
    fused_scores: dict[uuid.UUID, float] = {}
    rows_by_chunk_id: dict[uuid.UUID, tuple[Chunk, Document]] = {}

    for ranking in rankings:
        for rank, (chunk, document, _) in enumerate(ranking, start=1):
            fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1 / (k + rank)
            rows_by_chunk_id.setdefault(chunk.id, (chunk, document))

    merged = [(*rows_by_chunk_id[chunk_id], score) for chunk_id, score in fused_scores.items()]
    merged.sort(key=lambda row: row[2], reverse=True)
    return merged
