"""Tests for fetching the chunks next to a retrieved chunk.

In a module of its own rather than appended to ``test_retrieval.py``, which other
changes extend at the end of the file.
"""

import asyncio
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.auth import hash_api_key
from ragbridge.db.models import ApiKey
from ragbridge.retrieval import fetch_neighbours, keyword_search

THREE_BLOCKS = "\n\n".join(
    f"{word} block {n}: text written long enough that it stays a chunk of its own instead of"
    " being merged into a neighbouring chunk by the minimum chunk size."
    for n, word in enumerate(["Alpha", "Bravo", "Charlie"])
)
assert all(len(block) > 120 for block in THREE_BLOCKS.split("\n\n"))


def _neighbours_of_the_first_chunk(
    app: FastAPI, owner_key: str, asking_key: str, distance: int
) -> set[int]:
    """Retrieve the ``Alpha`` chunk as its owner, then fetch its neighbours as another tenant."""

    async def tenant_of(session: AsyncSession, key: str) -> uuid.UUID:
        api_key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
        assert api_key is not None
        return api_key.tenant_id

    async def run() -> set[int]:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        async with session_factory() as session:
            owner = await tenant_of(session, owner_key)
            asker = await tenant_of(session, asking_key)
            rows = await keyword_search(session, "Alpha", limit=1, tenant_id=owner)
            assert len(rows) == 1
            found = await fetch_neighbours(session, rows, distance=distance, tenant_id=asker)
        return {index for _, index in found}

    return asyncio.run(run())


def _upload(app: FastAPI, key: str) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    files = {"file": ("doc.txt", THREE_BLOCKS.encode(), "text/plain")}
    assert client.post("/documents", files=files).status_code == 201


def test_fetch_neighbours_returns_the_chunk_next_to_a_retrieved_one(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key)

    found = _neighbours_of_the_first_chunk(app_with_database, tenant_with_key, tenant_with_key, 1)

    assert found == {1}


def test_fetch_neighbours_reaches_as_far_as_asked(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key)

    found = _neighbours_of_the_first_chunk(app_with_database, tenant_with_key, tenant_with_key, 2)

    assert found == {1, 2}


def test_fetch_neighbours_never_returns_another_tenants_chunks(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key)

    found = _neighbours_of_the_first_chunk(
        app_with_database, tenant_with_key, second_tenant_with_key, 1
    )

    assert found == set()
