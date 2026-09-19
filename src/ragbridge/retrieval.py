"""Retrieval functions: turn a query into ranked chunks.

Two independent arms, kept as separate functions so each can be tested,
composed, and swapped on its own. ``reciprocal_rank_fusion`` merges their
results, and ``hybrid_search`` ties everything together into the one
function ``POST /query`` calls.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.db.models import Chunk, Document

SearchResult = tuple[Chunk, Document, float]
"""One retrieved chunk, its document, and a relevance score.

Higher is always better, for both arms - callers never need to know which
arm a score came from to compare it, even though the two arms compute a
score on entirely different scales (cosine similarity vs. ``ts_rank``).
"""


@dataclass(frozen=True)
class ChunkProvenance:
    """How one chunk got into the candidate set: which arm found it, and where.

    Kept beside ``SearchResult`` rather than inside it: ``SearchResult`` is
    a bare 3-tuple that the reranker and the agent loop read by position
    (decision 8, docs/plans/phase-6-ui.md), and widening it would rewrite
    all of them for a debugging field.
    """

    vector_rank: int | None
    """1-based position in the vector arm's results; ``None`` if it did not find the chunk."""
    keyword_rank: int | None
    """1-based position in the keyword arm's results; ``None`` if it did not find the chunk."""
    fused_score: float
    """The score retrieval returned: an RRF score in "hybrid" mode, else the arm's own score."""


async def vector_search(
    session: AsyncSession, embedding: list[float], limit: int, *, tenant_id: uuid.UUID
) -> list[SearchResult]:
    """Return ``tenant_id``'s chunks whose embedding is nearest to ``embedding``, nearest first.

    Score is ``1 - cosine_distance``: 1.0 for an exact match, lower for a
    less similar chunk - the same convention ``POST /query`` used in
    Phase 1, kept here so this is a pure refactor of that endpoint.

    Sets ``hnsw.iterative_scan`` for this query. pgvector's HNSW index is
    approximate: without it, a tenant-filtered search first walks the
    graph for its usual quota of nearest neighbours *across every
    tenant*, and only then throws away the rows that fail the filter -
    which under-returns candidates for a tenant whose data is a small
    slice of the table (decision 3, docs/plans/phase-3.md).
    ``relaxed_order`` makes the index keep walking until it has found
    ``limit`` matching rows instead (up to ``hnsw.max_scan_tuples``), at
    the cost of returning matches in a not-perfectly-distance-sorted
    order - an acceptable trade here, since ``reciprocal_rank_fusion``
    and the reranker both re-sort this candidate set anyway.
    """
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    distance = Chunk.embedding.cosine_distance(embedding).label("distance")
    result = await session.execute(
        select(Chunk, Document, distance)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.tenant_id == tenant_id)
        .order_by(distance)
        .limit(limit)
    )
    return [(chunk, document, 1 - distance) for chunk, document, distance in result.all()]


NATURAL_LANGUAGE_MIN_WORDS = 4
"""From this many words on, an unmarked query is treated as a question.

Shorter queries ("ERR_4021", "refund policy") are keywords, and every one of
them has to match: that precision is what makes an exact token findable.
"""

_WORD = re.compile(r"\w+")
_EXPLICIT_SYNTAX = re.compile(r'"|\bor\b|(?:^|\s)-\S', re.IGNORECASE)


def keyword_query(query: str) -> str:
    """Return the text ``keyword_search`` hands to ``websearch_to_tsquery``.

    ``websearch_to_tsquery`` requires *every* word of a query to be in one
    chunk. That suits a keyword, but a long natural-language question always
    contains words the answer does not (``work``, ``job``, ``start``): no
    chunk matches, the keyword arm returns nothing, and hybrid search
    silently becomes vector-only.

    So a query of ``NATURAL_LANGUAGE_MIN_WORDS`` or more words is rewritten
    as an OR of its words, which ``ts_rank`` then ranks by how many of them
    a chunk contains. Left exactly as typed: short queries, and anything
    with a quoted phrase, ``or`` or ``-word`` - the person asking for a
    specific behaviour.

    The rewrite uses ``websearch_to_tsquery``'s own ``or``, so stopwords
    and stemming are still handled by PostgreSQL. ``ts_rank`` does not know
    how common a word is, so a common word adds some noise; reciprocal rank
    fusion with the vector arm keeps that from deciding the result.
    """
    if _EXPLICIT_SYNTAX.search(query):
        return query
    words = _WORD.findall(query)
    if len(words) < NATURAL_LANGUAGE_MIN_WORDS:
        return query
    return " or ".join(words)


async def keyword_search(
    session: AsyncSession, query: str, limit: int, *, tenant_id: uuid.UUID
) -> list[SearchResult]:
    """Return ``tenant_id``'s chunks that best match ``query`` by full-text search, best first.

    Uses ``websearch_to_tsquery``, the parser built for text a user actually
    types (bare words, ``"quoted phrases"``, ``or``, ``-excluded``) - unlike
    ``to_tsquery`` it never raises on a plain sentence. The query text goes
    through ``keyword_query`` first, so a long question matches chunks that
    hold *some* of its words instead of none. Score is
    ``ts_rank``, PostgreSQL's own relevance measure for a tsquery match
    against a tsvector. The GIN index behind ``@@`` is an exact match, not
    an approximation, so - unlike ``vector_search`` - adding a tenant
    filter here needs no special handling to stay correct.
    """
    tsquery = func.websearch_to_tsquery("english", keyword_query(query))
    rank = func.ts_rank(Chunk.content_tsv, tsquery).label("rank")
    result = await session.execute(
        select(Chunk, Document, rank)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.content_tsv.op("@@")(tsquery), Chunk.tenant_id == tenant_id)
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


async def hybrid_search_with_provenance(
    session: AsyncSession,
    embedding: list[float],
    query: str,
    *,
    mode: Literal["hybrid", "vector", "keyword"],
    candidates: int,
    tenant_id: uuid.UUID,
) -> tuple[list[SearchResult], dict[uuid.UUID, ChunkProvenance]]:
    """Retrieve up to ``candidates`` of ``tenant_id``'s chunks for a question, using ``mode``.

    Returns up to ``candidates`` rows, not ``top_k``: narrowing the
    candidate set down to what a response actually returns is the
    reranker's job (see ``ragbridge.rerank``), not this function's - even
    when reranking is off, since the default reranker's whole job is that
    same narrowing step.

    "hybrid" runs ``vector_search`` then ``keyword_search``, each
    returning up to ``candidates`` rows, then merges them with
    ``reciprocal_rank_fusion``. The two queries run one after another, not
    concurrently: both go through the same ``AsyncSession``, and a single
    session can only have one query in flight at a time - the same rule
    as a single database connection, which is exactly what a session
    wraps. "vector" or "keyword" runs only that one arm.

    The second return value says, for every returned chunk, which arm
    found it and at what rank (``ChunkProvenance``). It is computed from
    the arms' own result lists, so it costs no extra query.
    """
    if mode == "vector":
        rows = await vector_search(session, embedding, candidates, tenant_id=tenant_id)
        vector_ranks = _ranks(rows)
        return rows, {
            chunk.id: ChunkProvenance(vector_ranks[chunk.id], None, score)
            for chunk, _, score in rows
        }
    if mode == "keyword":
        rows = await keyword_search(session, query, candidates, tenant_id=tenant_id)
        keyword_ranks = _ranks(rows)
        return rows, {
            chunk.id: ChunkProvenance(None, keyword_ranks[chunk.id], score)
            for chunk, _, score in rows
        }

    vector_rows = await vector_search(session, embedding, candidates, tenant_id=tenant_id)
    keyword_rows = await keyword_search(session, query, candidates, tenant_id=tenant_id)
    fused = reciprocal_rank_fusion([vector_rows, keyword_rows])
    vector_ranks = _ranks(vector_rows)
    keyword_ranks = _ranks(keyword_rows)
    return fused, {
        chunk.id: ChunkProvenance(vector_ranks.get(chunk.id), keyword_ranks.get(chunk.id), score)
        for chunk, _, score in fused
    }


async def hybrid_search(
    session: AsyncSession,
    embedding: list[float],
    query: str,
    *,
    mode: Literal["hybrid", "vector", "keyword"],
    candidates: int,
    tenant_id: uuid.UUID,
) -> list[SearchResult]:
    """Retrieve up to ``candidates`` chunks for a question, using ``mode``.

    The same retrieval as ``hybrid_search_with_provenance``, without the
    provenance. Every caller that does not need to explain itself uses this.
    """
    rows, _ = await hybrid_search_with_provenance(
        session, embedding, query, mode=mode, candidates=candidates, tenant_id=tenant_id
    )
    return rows


def _ranks(rows: Sequence[SearchResult]) -> dict[uuid.UUID, int]:
    """Map each chunk id to its 1-based position in ``rows``."""
    return {chunk.id: rank for rank, (chunk, _, _) in enumerate(rows, start=1)}
