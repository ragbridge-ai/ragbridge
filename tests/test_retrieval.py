"""Tests for the vector and keyword search functions, and their fusion."""

import asyncio
import uuid
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.auth import hash_api_key
from ragbridge.config import get_settings
from ragbridge.db.models import ApiKey, Chunk, Document
from ragbridge.embeddings import FakeEmbedder
from ragbridge.retrieval import (
    ChunkProvenance,
    SearchResult,
    hybrid_search,
    hybrid_search_with_provenance,
    keyword_search,
    or_fallback_query,
    reciprocal_rank_fusion,
    vector_search,
)


async def _tenant_id_for_key(
    session_factory: async_sessionmaker[AsyncSession], key: str
) -> uuid.UUID:
    """Resolve the tenant id behind a raw API key, for tests calling the
    retrieval functions directly - they need a tenant_id, not a header.
    """
    async with session_factory() as session:
        api_key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
        assert api_key is not None
        return api_key.tenant_id


def _make_chunk() -> Chunk:
    """A ``Chunk`` built in memory, never written to the database.

    ``reciprocal_rank_fusion`` is a pure function - it never touches a
    session - so its tests only need an object with an ``id``, the same
    way ``chunk_text`` (Phase 1 step 3) needed only plain strings.
    """
    return Chunk(id=uuid.uuid4(), document_id=uuid.uuid4(), chunk_index=0, content="", metadata_={})


def _make_document() -> Document:
    return Document(
        id=uuid.uuid4(), filename="a.txt", content_type="text/plain", sha256="x", content="x"
    )


def test_keyword_search_finds_a_literal_token_that_vector_search_misses(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The gap the second retrieval arm exists to close.

    ``FakeEmbedder`` has no semantics - it turns each distinct text into an
    unrelated random vector - so nothing here claims vector search is
    "bad". It shows the specific failure mode hybrid search targets: a
    rare, literal token (an error code) that keyword search matches
    exactly and that a semantics-free embedder has no way to connect to
    the question.
    """
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    error_chunk = "Error code ERR_4021 means the upload exceeded the size limit."
    other_chunk = "Cats are independent and curious animals."
    client.post("/documents", files={"file": ("errors.txt", error_chunk.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", other_chunk.encode(), "text/plain")})

    async def run_both_arms() -> tuple[list[str], list[str]]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        tenant_id = await _tenant_id_for_key(session_factory, tenant_with_key)
        dimension = get_settings().embedding_dimension
        [embedding] = await FakeEmbedder(dimension).embed(["ERR_4021"])
        async with session_factory() as session:
            vector_rows = await vector_search(session, embedding, limit=5, tenant_id=tenant_id)
            keyword_rows = await keyword_search(session, "ERR_4021", limit=5, tenant_id=tenant_id)
        return (
            [chunk.content for chunk, _, _ in vector_rows],
            [chunk.content for chunk, _, _ in keyword_rows],
        )

    vector_order, keyword_order = asyncio.run(run_both_arms())

    assert keyword_order[0] == error_chunk
    assert vector_order[0] != error_chunk


def test_keyword_search_excludes_chunks_with_no_matching_terms(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """``websearch_to_tsquery`` filters with ``@@``, it does not just rank.

    A chunk sharing no terms with the query must not appear at all, unlike
    vector search where every row gets some (possibly meaningless) score.
    """
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    client.post(
        "/documents",
        files={"file": ("a.txt", b"Cats are independent and curious animals.", "text/plain")},
    )

    async def run_keyword_search() -> list[str]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        tenant_id = await _tenant_id_for_key(session_factory, tenant_with_key)
        async with session_factory() as session:
            rows = await keyword_search(session, "ERR_4021", limit=5, tenant_id=tenant_id)
        return [chunk.content for chunk, _, _ in rows]

    results = asyncio.run(run_keyword_search())

    assert results == []


def test_reciprocal_rank_fusion_favors_agreement_over_a_single_arms_top_pick() -> None:
    """A chunk both arms rank around the middle beats one arm's favorite.

    Concrete numbers from docs/plans/phase-2.md step 2: rank 1 in one
    ranking and rank 8 in the other scores 1/61 + 1/68 ≈ 0.0311; rank 3 in
    both scores 1/63 + 1/63 ≈ 0.0317 and wins - RRF rewards a chunk both
    arms consider reasonably relevant over a chunk one arm loves and the
    other barely surfaces.
    """
    document = _make_document()
    top_once = _make_chunk()
    mid_twice = _make_chunk()
    fillers = [_make_chunk() for _ in range(7)]

    ranking_one: list[SearchResult] = [
        (top_once, document, 0.0),  # rank 1
        (fillers[0], document, 0.0),  # rank 2
        (mid_twice, document, 0.0),  # rank 3
    ]
    ranking_two: list[SearchResult] = [
        (fillers[1], document, 0.0),  # rank 1
        (fillers[2], document, 0.0),  # rank 2
        (mid_twice, document, 0.0),  # rank 3
        (fillers[3], document, 0.0),  # rank 4
        (fillers[4], document, 0.0),  # rank 5
        (fillers[5], document, 0.0),  # rank 6
        (fillers[6], document, 0.0),  # rank 7
        (top_once, document, 0.0),  # rank 8
    ]

    fused = reciprocal_rank_fusion([ranking_one, ranking_two])

    fused_ids = [chunk.id for chunk, _, _ in fused]
    assert fused_ids[0] == mid_twice.id
    assert fused_ids[1] == top_once.id

    scores = {chunk.id: score for chunk, _, score in fused}
    assert scores[top_once.id] == pytest.approx(1 / 61 + 1 / 68)
    assert scores[mid_twice.id] == pytest.approx(1 / 63 + 1 / 63)


def test_reciprocal_rank_fusion_includes_items_found_by_only_one_arm() -> None:
    document = _make_document()
    only_in_first = _make_chunk()
    only_in_second = _make_chunk()

    fused = reciprocal_rank_fusion(
        [
            [(only_in_first, document, 0.0)],
            [(only_in_second, document, 0.0)],
        ]
    )

    fused_ids = {chunk.id for chunk, _, _ in fused}
    assert fused_ids == {only_in_first.id, only_in_second.id}
    for _, _, score in fused:
        assert score == pytest.approx(1 / 61)


def test_reciprocal_rank_fusion_of_no_rankings_returns_empty_list() -> None:
    assert reciprocal_rank_fusion([]) == []


ERROR_CHUNK = "Error code ERR_4021 means the upload exceeded the size limit."
OTHER_CHUNK = "Cats are independent and curious animals."


def _provenance_for(
    app: FastAPI, key: str, query: str, mode: Literal["hybrid", "vector", "keyword"]
) -> tuple[list[SearchResult], dict[uuid.UUID, ChunkProvenance]]:
    """Upload the two fixed chunks, then run ``hybrid_search_with_provenance`` on ``query``."""
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    client.post("/documents", files={"file": ("errors.txt", ERROR_CHUNK.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", OTHER_CHUNK.encode(), "text/plain")})

    async def run() -> tuple[list[SearchResult], dict[uuid.UUID, ChunkProvenance]]:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        tenant_id = await _tenant_id_for_key(session_factory, key)
        [embedding] = await FakeEmbedder(get_settings().embedding_dimension).embed([query])
        async with session_factory() as session:
            return await hybrid_search_with_provenance(
                session, embedding, query, mode=mode, candidates=10, tenant_id=tenant_id
            )

    return asyncio.run(run())


def test_provenance_records_both_ranks_for_a_chunk_both_arms_found(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """FakeEmbedder gives identical text an identical vector, so querying the
    exact chunk text makes it the nearest by embedding *and* the only
    keyword match: rank 1 in both arms, fused score 1/61 + 1/61.
    """
    rows, provenance = _provenance_for(
        app_with_database, tenant_with_key, ERROR_CHUNK, mode="hybrid"
    )

    [error_row] = [row for row in rows if row[0].content == ERROR_CHUNK]
    found = provenance[error_row[0].id]
    assert found.vector_rank == 1
    assert found.keyword_rank == 1
    assert found.fused_score == pytest.approx(1 / 61 + 1 / 61)


def test_provenance_leaves_the_keyword_rank_empty_for_a_vector_only_chunk(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The cats chunk shares no term with the query, so only the vector arm
    (which returns every chunk, however far) can have found it.
    """
    rows, provenance = _provenance_for(
        app_with_database, tenant_with_key, ERROR_CHUNK, mode="hybrid"
    )

    [cats_row] = [row for row in rows if row[0].content == OTHER_CHUNK]
    found = provenance[cats_row[0].id]
    assert found.keyword_rank is None
    assert found.vector_rank == 2


def test_provenance_scores_and_order_match_reciprocal_rank_fusion(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    rows, provenance = _provenance_for(
        app_with_database, tenant_with_key, ERROR_CHUNK, mode="hybrid"
    )

    assert set(provenance) == {chunk.id for chunk, _, _ in rows}
    assert [provenance[chunk.id].fused_score for chunk, _, _ in rows] == [
        score for _, _, score in rows
    ]


def test_provenance_in_vector_mode_has_no_keyword_ranks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    rows, provenance = _provenance_for(
        app_with_database, tenant_with_key, ERROR_CHUNK, mode="vector"
    )

    assert [provenance[chunk.id].vector_rank for chunk, _, _ in rows] == [1, 2]
    assert all(found.keyword_rank is None for found in provenance.values())
    assert provenance[rows[0][0].id].fused_score == rows[0][2] == 1.0


def test_provenance_in_keyword_mode_has_no_vector_ranks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    rows, provenance = _provenance_for(
        app_with_database, tenant_with_key, "ERR_4021", mode="keyword"
    )

    assert [provenance[chunk.id].keyword_rank for chunk, _, _ in rows] == [1]
    assert all(found.vector_rank is None for found in provenance.values())


def test_hybrid_search_returns_the_same_rows_as_the_provenance_version(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """``hybrid_search`` is now a wrapper; its callers must see no difference."""
    with_provenance, _ = _provenance_for(
        app_with_database, tenant_with_key, ERROR_CHUNK, mode="hybrid"
    )

    async def plain() -> list[SearchResult]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        tenant_id = await _tenant_id_for_key(session_factory, tenant_with_key)
        [embedding] = await FakeEmbedder(get_settings().embedding_dimension).embed([ERROR_CHUNK])
        async with session_factory() as session:
            return await hybrid_search(
                session, embedding, ERROR_CHUNK, mode="hybrid", candidates=10, tenant_id=tenant_id
            )

    assert [(c.id, score) for c, _, score in asyncio.run(plain())] == [
        (c.id, score) for c, _, score in with_provenance
    ]


# --- the keyword arm falls back to an OR of the words --------------------------------


def test_the_fallback_is_an_or_of_the_words_of_the_query() -> None:
    assert or_fallback_query("docker container orchestration") == (
        "docker or container or orchestration"
    )


def test_the_fallback_keeps_identifiers_whole_and_drops_punctuation() -> None:
    assert or_fallback_query("ERR_4021, upload failed!") == "ERR_4021 or upload or failed"


def test_a_typed_or_is_not_kept_as_a_word_of_the_fallback() -> None:
    assert or_fallback_query("refund OR return policy") == "refund or return or policy"


def test_a_single_word_has_no_fallback() -> None:
    """An OR of one word is the same query."""
    assert or_fallback_query("ERR_4021") is None


@pytest.mark.parametrize(
    "query", ['"refund policy" damaged', "refund policy -shipping", 'a "b c"', "x y -z"]
)
def test_a_quoted_phrase_or_an_exclusion_has_no_fallback(query: str) -> None:
    """Dropping a phrase or an exclusion would return what the person ruled out."""
    assert or_fallback_query(query) is None


LONG_QUESTION = "How are invoices reconciled, and are refunds for damaged parcels approved quickly?"
INVOICE_CHUNK = "Invoices are reconciled automatically every month by the billing service."
REFUND_CHUNK = "Refunds for damaged parcels are approved within five days."
ARCHIVE_CHUNK = "Invoices for damaged parcels are archived after a year."
DOCKER_CHUNK = "Services are packaged as Docker images and released with one container tool."


def _upload(client: TestClient, name: str, text: str) -> None:
    client.post("/documents", files={"file": (name, text.encode(), "text/plain")})


def _keyword_contents(app: FastAPI, key: str, query: str) -> list[str]:
    async def run() -> list[str]:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        tenant_id = await _tenant_id_for_key(session_factory, key)
        async with session_factory() as session:
            rows = await keyword_search(session, query, limit=10, tenant_id=tenant_id)
        return [chunk.content for chunk, _, _ in rows]

    return asyncio.run(run())


def _client_with(app: FastAPI, key: str, chunks: dict[str, str]) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    for name, text in chunks.items():
        _upload(client, name, text)


def test_a_question_spread_over_two_chunks_finds_both_in_the_keyword_arm(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """No chunk holds every word of the question, so the strict query finds nothing;
    the fallback finds the chunks that hold some of the words.
    """
    _client_with(
        app_with_database, tenant_with_key, {"a.txt": INVOICE_CHUNK, "b.txt": REFUND_CHUNK}
    )

    found = _keyword_contents(app_with_database, tenant_with_key, LONG_QUESTION)

    assert set(found) == {INVOICE_CHUNK, REFUND_CHUNK}


def test_a_query_with_a_word_that_appears_nowhere_still_finds_its_other_words(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The reported case: three words, one of them in no document, used to find nothing."""
    _client_with(
        app_with_database, tenant_with_key, {"a.txt": INVOICE_CHUNK, "b.txt": REFUND_CHUNK}
    )

    found = _keyword_contents(app_with_database, tenant_with_key, "invoices refunds bananas")

    assert set(found) == {INVOICE_CHUNK, REFUND_CHUNK}


def test_a_natural_question_containing_or_still_finds_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """A typed ``or`` used to make the whole query strict, so nothing matched."""
    _client_with(
        app_with_database, tenant_with_key, {"a.txt": DOCKER_CHUNK, "b.txt": INVOICE_CHUNK}
    )
    question = (
        "Which company did the candidate use Docker at, and did they use Kubernetes"
        " or another container tool there?"
    )

    assert _keyword_contents(app_with_database, tenant_with_key, question) == [DOCKER_CHUNK]


def test_a_query_that_matches_strictly_is_not_loosened(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Both words are in the invoice chunk, so only it is returned, although another
    chunk holds one of them: the OR fallback runs only when nothing matched.
    """
    _client_with(
        app_with_database,
        tenant_with_key,
        {"a.txt": INVOICE_CHUNK, "b.txt": ARCHIVE_CHUNK, "c.txt": REFUND_CHUNK},
    )

    assert _keyword_contents(app_with_database, tenant_with_key, "invoices reconciled") == [
        INVOICE_CHUNK
    ]


def test_a_quoted_phrase_that_matches_nothing_is_not_turned_into_an_or(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _client_with(app_with_database, tenant_with_key, {"a.txt": REFUND_CHUNK})

    assert _keyword_contents(app_with_database, tenant_with_key, '"parcels damaged"') == []


def test_an_exclusion_is_not_dropped_by_the_fallback(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Every chunk about refunds also says "damaged", so this finds nothing; a fallback
    that ignored the exclusion would return the very chunk the person ruled out.
    """
    _client_with(app_with_database, tenant_with_key, {"a.txt": REFUND_CHUNK})

    assert _keyword_contents(app_with_database, tenant_with_key, "refunds -damaged") == []


def test_a_chunk_found_by_both_arms_outscores_any_single_arm_chunk(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The question has words the chunk lacks ("server", "say", "big"), so it
    used to reach the keyword arm as a query nothing matched.
    """
    question = "Why does the server say error code ERR_4021 when my upload is too big?"

    rows, provenance = _provenance_for(app_with_database, tenant_with_key, question, mode="hybrid")

    [error_row] = [row for row in rows if row[0].content == ERROR_CHUNK]
    found = provenance[error_row[0].id]
    assert found.keyword_rank == 1
    assert found.vector_rank is not None
    single_arm = [
        p.fused_score
        for p in provenance.values()
        if (p.vector_rank is None) != (p.keyword_rank is None)
    ]
    assert single_arm, "the other chunk should be found by one arm only"
    assert found.fused_score > max(single_arm)
    assert found.fused_score > 1 / 61


def test_a_short_query_with_an_absent_word_still_gets_a_chunk_found_by_both_arms(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """``bananas`` is in no chunk, so the strict keyword query used to find nothing."""
    rows, provenance = _provenance_for(
        app_with_database, tenant_with_key, "ERR_4021 bananas", mode="hybrid"
    )

    [error_row] = [row for row in rows if row[0].content == ERROR_CHUNK]
    found = provenance[error_row[0].id]
    assert found.keyword_rank == 1
    assert found.vector_rank is not None
    single_arm = [
        p.fused_score
        for p in provenance.values()
        if (p.vector_rank is None) != (p.keyword_rank is None)
    ]
    assert found.fused_score > max(single_arm, default=0.0)
    assert found.fused_score > 1 / 61
