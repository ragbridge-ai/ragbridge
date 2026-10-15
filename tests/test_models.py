"""Tests for ORM models, against the real test database."""

import asyncio
import hashlib
import uuid

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


def _document(
    tenant_id: uuid.UUID, content: str, external_id: str | None = None, filename: str = "d.txt"
) -> Document:
    return Document(
        tenant_id=tenant_id,
        filename=filename,
        content_type="text/plain",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
        external_id=external_id,
    )


async def _insert_in_fresh_sessions(
    app: FastAPI, batches: list[list[Document]]
) -> IntegrityError | None:
    """Commit each batch in its own session; return the first IntegrityError, if any."""
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    for batch in batches:
        async with session_factory() as session:
            session.add_all(batch)
            try:
                await session.commit()
            except IntegrityError as error:
                return error
    return None


async def _new_tenant(app: FastAPI, name: str) -> uuid.UUID:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        tenant = Tenant(name=name)
        session.add(tenant)
        await session.commit()
        return tenant.id


def test_two_synced_documents_may_hold_identical_text(app_with_database: FastAPI) -> None:
    async def run() -> IntegrityError | None:
        tenant_id = await _new_tenant(app_with_database, "acme")
        return await _insert_in_fresh_sessions(
            app_with_database,
            [
                [_document(tenant_id, "Same boilerplate.", external_id="post:1")],
                [_document(tenant_id, "Same boilerplate.", external_id="post:2")],
            ],
        )

    assert asyncio.run(run()) is None


def test_a_synced_document_and_an_upload_may_hold_identical_text(
    app_with_database: FastAPI,
) -> None:
    async def run() -> IntegrityError | None:
        tenant_id = await _new_tenant(app_with_database, "acme")
        return await _insert_in_fresh_sessions(
            app_with_database,
            [
                [_document(tenant_id, "Shared text.")],
                [_document(tenant_id, "Shared text.", external_id="post:1")],
            ],
        )

    assert asyncio.run(run()) is None


def test_two_uploads_with_identical_text_still_conflict(app_with_database: FastAPI) -> None:
    async def run() -> IntegrityError | None:
        tenant_id = await _new_tenant(app_with_database, "acme")
        return await _insert_in_fresh_sessions(
            app_with_database,
            [[_document(tenant_id, "Same bytes.")], [_document(tenant_id, "Same bytes.")]],
        )

    error = asyncio.run(run())

    assert error is not None
    assert "uq_documents_tenant_id_sha256_uploads" in str(error)


def test_an_external_id_is_unique_within_a_tenant(app_with_database: FastAPI) -> None:
    async def run() -> IntegrityError | None:
        tenant_id = await _new_tenant(app_with_database, "acme")
        return await _insert_in_fresh_sessions(
            app_with_database,
            [
                [_document(tenant_id, "First.", external_id="post:1")],
                [_document(tenant_id, "Second.", external_id="post:1")],
            ],
        )

    error = asyncio.run(run())

    assert error is not None
    assert "uq_documents_tenant_id_external_id" in str(error)


def test_the_same_external_id_may_exist_in_two_tenants(app_with_database: FastAPI) -> None:
    async def run() -> IntegrityError | None:
        first = await _new_tenant(app_with_database, "acme")
        second = await _new_tenant(app_with_database, "globex")
        return await _insert_in_fresh_sessions(
            app_with_database,
            [
                [_document(first, "One.", external_id="post:1")],
                [_document(second, "Two.", external_id="post:1")],
            ],
        )

    assert asyncio.run(run()) is None


def test_a_new_document_has_empty_metadata_and_no_external_id(app_with_database: FastAPI) -> None:
    async def run() -> Document:
        tenant_id = await _new_tenant(app_with_database, "acme")
        await _insert_in_fresh_sessions(app_with_database, [[_document(tenant_id, "Plain.")]])
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        async with session_factory() as session:
            return (await session.execute(select(Document))).scalar_one()

    stored = asyncio.run(run())

    assert stored.external_id is None
    assert stored.metadata_ == {}
    assert stored.source_updated_at is None
    assert stored.updated_at is not None


def test_access_groups_round_trip_and_default_to_empty(app_with_database: FastAPI) -> None:
    async def run() -> tuple[Document, Document, Chunk, Chunk]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        dimension = get_settings().embedding_dimension
        async with session_factory() as session:
            tenant = Tenant(name="acme")
            session.add(tenant)
            await session.flush()
            documents = [
                Document(
                    tenant_id=tenant.id,
                    filename=name,
                    content_type="text/plain",
                    sha256=name,
                    access_groups=groups,
                )
                for name, groups in (("labelled", ["hr", "public"]), ("plain", None))
                if groups is not None
            ] + [
                Document(
                    tenant_id=tenant.id, filename="plain", content_type="text/plain", sha256="p"
                )
            ]
            session.add_all(documents)
            await session.flush()
            chunks = [
                Chunk(
                    document_id=documents[0].id,
                    tenant_id=tenant.id,
                    chunk_index=0,
                    content="a",
                    embedding=[0.0] * dimension,
                    access_groups=["hr"],
                ),
                Chunk(
                    document_id=documents[1].id,
                    tenant_id=tenant.id,
                    chunk_index=0,
                    content="b",
                    embedding=[0.0] * dimension,
                ),
            ]
            session.add_all(chunks)
            await session.commit()
            ids = [document.id for document in documents], [chunk.id for chunk in chunks]
        async with session_factory() as session:
            stored_documents = [await session.get(Document, id_) for id_ in ids[0]]
            stored_chunks = [await session.get(Chunk, id_) for id_ in ids[1]]
        assert all(item is not None for item in (*stored_documents, *stored_chunks))
        labelled, plain = stored_documents
        labelled_chunk, plain_chunk = stored_chunks
        assert labelled is not None and plain is not None
        assert labelled_chunk is not None and plain_chunk is not None
        return labelled, plain, labelled_chunk, plain_chunk

    labelled, plain, labelled_chunk, plain_chunk = asyncio.run(run())

    assert labelled.access_groups == ["hr", "public"]
    assert plain.access_groups == []
    assert labelled_chunk.access_groups == ["hr"]
    assert plain_chunk.access_groups == []


def test_an_api_key_does_not_require_an_access_scope_unless_marked(
    app_with_database: FastAPI,
) -> None:
    async def run() -> tuple[bool, bool]:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        async with session_factory() as session:
            tenant = Tenant(name="acme")
            session.add(tenant)
            await session.flush()
            keys = [
                ApiKey(tenant_id=tenant.id, key_hash=name, prefix=name, name=name, **flag)
                for name, flag in (("plain", {}), ("scoped", {"requires_access_scope": True}))
            ]
            session.add_all(keys)
            await session.commit()
            ids = [key.id for key in keys]
        async with session_factory() as session:
            plain, scoped = [await session.get(ApiKey, id_) for id_ in ids]
        assert plain is not None and scoped is not None
        return plain.requires_access_scope, scoped.requires_access_scope

    assert asyncio.run(run()) == (False, True)
