"""Tests for limiting every read of chunks to a caller's access groups.

Each read that returns chunk text - the vector arm, the keyword arm in both of its forms,
the keyword statistics and the neighbours - gets the same four checks: a caller with the
group sees the document, a caller with another group does not, a caller with no groups sees
nothing, and a caller that sent no groups at all (``None``) sees everything, as before.
"""

import asyncio
import uuid
from collections.abc import Sequence
from typing import Literal

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.config import get_settings
from ragbridge.db.models import Chunk, Document, Tenant
from ragbridge.embeddings import FakeEmbedder
from ragbridge.retrieval import (
    fetch_neighbours,
    hybrid_search,
    keyword_search,
    vector_search,
)

Arm = Literal["vector", "keyword", "keyword_or_fallback", "hybrid"]


async def _add_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    texts: list[str],
    groups: list[str],
    *,
    chunk_groups: list[list[str]] | None = None,
) -> Document:
    """A document and its chunks, written straight to the database.

    ``chunk_groups`` overrides the groups of each chunk, to build a document whose chunks
    disagree with their document (the filter is on the chunks, so it must hold then too).
    """
    document = Document(
        tenant_id=tenant_id,
        filename=name,
        content_type="text/plain",
        sha256=name,
        access_groups=groups,
        status="ready",
    )
    session.add(document)
    await session.flush()
    embeddings = await FakeEmbedder(get_settings().embedding_dimension).embed(texts)
    session.add_all(
        Chunk(
            document_id=document.id,
            tenant_id=tenant_id,
            chunk_index=index,
            content=text,
            embedding=embedding,
            access_groups=chunk_groups[index] if chunk_groups else groups,
        )
        for index, (text, embedding) in enumerate(zip(texts, embeddings, strict=True))
    )
    await session.flush()
    return document


async def _new_tenant(session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(name="acme")
    session.add(tenant)
    await session.flush()
    return tenant.id


async def _seed_policies(session: AsyncSession) -> uuid.UUID:
    tenant_id = await _new_tenant(session)
    await _add_document(
        session, tenant_id, "public", ["The refund policy for visitors."], ["public"]
    )
    await _add_document(session, tenant_id, "hr", ["The salary policy for staff."], ["hr"])
    await _add_document(
        session, tenant_id, "both", ["The leave policy for everyone."], ["hr", "public"]
    )
    await _add_document(session, tenant_id, "nobody", ["The draft policy, not shared."], [])
    await session.commit()
    return tenant_id


async def _names_found(
    session: AsyncSession, arm: Arm, tenant_id: uuid.UUID, groups: Sequence[str] | None
) -> set[str]:
    [embedding] = await FakeEmbedder(get_settings().embedding_dimension).embed(["policy"])
    if arm == "vector":
        rows = await vector_search(
            session, embedding, 10, tenant_id=tenant_id, access_groups=groups
        )
    elif arm == "keyword":
        rows = await keyword_search(
            session, "policy", 10, tenant_id=tenant_id, access_groups=groups
        )
    elif arm == "keyword_or_fallback":
        # "nonexistentword" is in no chunk, so the query as typed finds nothing and the
        # OR version runs.
        rows = await keyword_search(
            session, "policy nonexistentword", 10, tenant_id=tenant_id, access_groups=groups
        )
    else:
        rows = await hybrid_search(
            session,
            embedding,
            "policy",
            mode="hybrid",
            candidates=10,
            tenant_id=tenant_id,
            access_groups=groups,
        )
    return {document.filename for _, document, _ in rows}


@pytest.mark.parametrize("arm", ["vector", "keyword", "keyword_or_fallback", "hybrid"])
@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        (["public"], {"public", "both"}),
        (["hr"], {"hr", "both"}),
        (["hr", "public"], {"public", "hr", "both"}),
        (["unknown"], set()),
        ([], set()),
        (None, {"public", "hr", "both", "nobody"}),
    ],
)
def test_a_read_returns_only_chunks_sharing_one_of_the_groups(
    app_with_database: FastAPI, arm: Arm, groups: list[str] | None, expected: set[str]
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def run() -> set[str]:
        async with session_factory() as session:
            tenant_id = await _seed_policies(session)
            return await _names_found(session, arm, tenant_id, groups)

    assert asyncio.run(run()) == expected


def test_a_document_without_groups_is_never_returned_for_any_list(
    app_with_database: FastAPI,
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def run() -> set[str]:
        async with session_factory() as session:
            tenant_id = await _seed_policies(session)
            found: set[str] = set()
            for arm in ("vector", "keyword", "keyword_or_fallback", "hybrid"):
                for groups in (["public"], ["hr"], ["public", "hr", "nobody", ""]):
                    found |= await _names_found(session, arm, tenant_id, groups)
            return found

    assert "nobody" not in asyncio.run(run())


def test_neighbours_are_limited_to_the_groups_even_when_they_differ_from_the_chunk(
    app_with_database: FastAPI,
) -> None:
    """A document's chunks normally share its groups, so this is the second layer: the
    middle chunk is labelled ``hr`` by hand and must not be handed to a ``public`` caller.
    """
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def neighbours(groups: list[str] | None) -> set[int]:
        async with session_factory() as session:
            tenant_id = await _new_tenant(session)
            document = await _add_document(
                session,
                tenant_id,
                "doc",
                ["first chunk", "middle chunk", "last chunk"],
                ["public"],
                chunk_groups=[["public"], ["hr"], ["public"]],
            )
            first = await session.scalar(
                select(Chunk).where(Chunk.document_id == document.id, Chunk.chunk_index == 0)
            )
            assert first is not None
            rows = [(first, document, 1.0)]
            found = await fetch_neighbours(
                session, rows, distance=1, tenant_id=tenant_id, access_groups=groups
            )
        return {index for _, index in found}

    assert asyncio.run(neighbours(["public"])) == set()
    assert asyncio.run(neighbours(["hr"])) == {1}
    assert asyncio.run(neighbours(None)) == {1}
    assert asyncio.run(neighbours([])) == set()


HIDDEN_ZEBRA_CHUNKS = 40
PUBLIC_FILLER_CHUNKS = 18


async def _seed_hidden_common_word(session: AsyncSession) -> uuid.UUID:
    """40 ``hr`` chunks that all say "zebra"; 20 ``public`` chunks, one of which says
    "zebra giraffe" and one "giraffe" alone.

    Counted over the whole tenant, "zebra" is in two thirds of the chunks and is dropped
    from a query as too common. Counted over the ``public`` chunks it is in one in twenty.
    """
    tenant_id = await _new_tenant(session)
    await _add_document(
        session,
        tenant_id,
        "hidden",
        [f"zebra note {index}" for index in range(HIDDEN_ZEBRA_CHUNKS)],
        ["hr"],
    )
    await _add_document(
        session,
        tenant_id,
        "visible",
        [
            "zebra giraffe sighting",
            "giraffe only here",
            *[f"plain filler {index}" for index in range(PUBLIC_FILLER_CHUNKS)],
        ],
        ["public"],
    )
    await session.commit()
    return tenant_id


def test_word_frequency_is_counted_over_the_chunks_the_caller_may_see(
    app_with_database: FastAPI,
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def contents(groups: list[str] | None) -> set[str]:
        async with session_factory() as session:
            tenant_id = await _seed_hidden_common_word(session)
            rows = await keyword_search(
                session,
                "zebra giraffe",
                100,
                tenant_id=tenant_id,
                access_groups=groups,
                max_term_share=0.5,
            )
        return {chunk.content for chunk, _, _ in rows}

    assert asyncio.run(contents(["public"])) == {"zebra giraffe sighting"}
    # The control: with no scope the same query counts every chunk, "zebra" is dropped as
    # too common, and the second chunk matches as well. It shows the scoped result above
    # is not an accident of the data.
    assert asyncio.run(contents(None)) == {"zebra giraffe sighting", "giraffe only here"}


def test_the_rarity_weighted_fallback_returns_only_the_callers_chunks(
    app_with_database: FastAPI,
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def names(groups: list[str] | None) -> set[str]:
        async with session_factory() as session:
            tenant_id = await _seed_hidden_common_word(session)
            rows = await keyword_search(
                session,
                "zebra nonexistentword",
                100,
                tenant_id=tenant_id,
                access_groups=groups,
                rarity_weighting=True,
            )
        return {document.filename for _, document, _ in rows}

    assert asyncio.run(names(["public"])) == {"visible"}
    assert asyncio.run(names(None)) == {"visible", "hidden"}
