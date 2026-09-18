"""Retrieval functions: turn a query into ranked chunks.

Two independent arms, kept as separate functions so each can be tested,
composed, and swapped on its own. Phase 2 step 2 fuses their results with
Reciprocal Rank Fusion; this step only gives each arm a home and a shared
return shape, so fusion has something uniform to work with.
"""

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
