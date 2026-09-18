"""Tests for ORM models, against the real test database."""

import asyncio
import hashlib

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.config import get_settings
from ragbridge.db.models import ApiKey, Chunk, Document, Tenant


def test_document_round_trip(app_with_database: FastAPI) -> None:
    async def create_and_read_back() -> Document:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        content = "Hello, ragbridge."

        async with session_factory() as session:
            tenant = Tenant(name="acme")
            session.add(tenant)
            await session.flush()

            document = Document(
                tenant_id=tenant.id,
                filename="hello.txt",
                content_type="text/plain",
                sha256=hashlib.sha256(content.encode()).hexdigest(),
                content=content,
            )
            session.add(document)
            await session.commit()
            document_id = document.id

        async with session_factory() as session:
            result = await session.execute(select(Document).where(Document.id == document_id))
            return result.scalar_one()

    stored = asyncio.run(create_and_read_back())

    assert stored.filename == "hello.txt"
    assert stored.content_type == "text/plain"
    assert stored.content == "Hello, ragbridge."
    assert stored.created_at is not None


def test_chunk_round_trip(app_with_database: FastAPI) -> None:
    async def create_and_read_back() -> Chunk:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        content = "Hello, ragbridge."
        dimension = get_settings().embedding_dimension

        async with session_factory() as session:
            tenant = Tenant(name="acme")
            session.add(tenant)
            await session.flush()

            document = Document(
                tenant_id=tenant.id,
                filename="hello.txt",
                content_type="text/plain",
                sha256=hashlib.sha256(content.encode()).hexdigest(),
                content=content,
            )
            session.add(document)
            await session.flush()

            chunk = Chunk(
                document_id=document.id,
                tenant_id=tenant.id,
                chunk_index=0,
                content=content,
                embedding=[0.0] * dimension,
                metadata_={"page": 1},
            )
            session.add(chunk)
            await session.commit()
            chunk_id = chunk.id

        async with session_factory() as session:
            result = await session.execute(select(Chunk).where(Chunk.id == chunk_id))
            return result.scalar_one()

    stored = asyncio.run(create_and_read_back())

    assert stored.chunk_index == 0
    assert stored.content == "Hello, ragbridge."
    assert len(stored.embedding) == get_settings().embedding_dimension
    assert stored.metadata_ == {"page": 1}
    assert stored.content_tsv == "'hello':1 'ragbridg':2"


def test_tenant_and_api_key_round_trip(app_with_database: FastAPI) -> None:
    async def create_and_read_back() -> ApiKey:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

        async with session_factory() as session:
            tenant = Tenant(name="acme")
            session.add(tenant)
            await session.flush()

            api_key = ApiKey(
                tenant_id=tenant.id,
                key_hash=hashlib.sha256(b"rb_secret").hexdigest(),
                prefix="rb_secret"[:11],
                name="default",
            )
            session.add(api_key)
            await session.commit()
            api_key_id = api_key.id

        async with session_factory() as session:
            result = await session.execute(select(ApiKey).where(ApiKey.id == api_key_id))
            return result.scalar_one()

    stored = asyncio.run(create_and_read_back())

    assert stored.prefix == "rb_secret"
    assert stored.last_used_at is None
    assert stored.revoked_at is None


def test_deleting_tenant_cascades_to_api_keys(app_with_database: FastAPI) -> None:
    async def create_tenant_then_delete_it() -> ApiKey | None:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

        async with session_factory() as session:
            tenant = Tenant(name="acme")
            session.add(tenant)
            await session.flush()

            api_key = ApiKey(
                tenant_id=tenant.id,
                key_hash=hashlib.sha256(b"rb_secret").hexdigest(),
                prefix="rb_secret"[:11],
                name="default",
            )
            session.add(api_key)
            await session.commit()
            api_key_id = api_key.id

        async with session_factory() as session:
            tenant_to_delete = await session.get(Tenant, tenant.id)
            assert tenant_to_delete is not None
            await session.delete(tenant_to_delete)
            await session.commit()

        async with session_factory() as session:
            result = await session.execute(select(ApiKey).where(ApiKey.id == api_key_id))
            return result.scalar_one_or_none()

    assert asyncio.run(create_tenant_then_delete_it()) is None
