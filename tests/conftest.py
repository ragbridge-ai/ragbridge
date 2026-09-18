"""Shared fixtures for tests that need a database-backed app."""

import asyncio

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.auth import api_key_prefix, generate_api_key, hash_api_key
from ragbridge.chat import FakeChatter, get_chatter
from ragbridge.config import Settings
from ragbridge.db.models import ApiKey, Tenant
from ragbridge.db.session import create_engine, create_session_factory
from ragbridge.embeddings import FakeEmbedder, get_embedder
from ragbridge.main import create_app


@pytest.fixture(autouse=True)
def _reset_database() -> None:
    """Empty every table before each test.

    Tests run against the real test database (see decision 5 in
    docs/plans/phase-1.md), not an in-memory fake, so rows written by one
    test would otherwise still be there for the next one - for example
    two tests uploading a document with the same content would collide
    on the unique ``sha256`` constraint.
    """

    async def truncate_all_tables() -> None:
        engine = create_engine(Settings())
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE chunks, documents, api_keys, tenants CASCADE")
            )
        await engine.dispose()

    asyncio.run(truncate_all_tables())


@pytest.fixture
def app_with_database() -> FastAPI:
    """A FastAPI app wired to the real test database.

    Reads ``Settings`` the normal way (environment, then ``.env``, then the
    default), so it points at whatever database is actually available -
    docker-compose locally, the ``postgres`` service container in CI.
    """
    app = create_app()
    settings = Settings()
    engine = create_engine(settings)
    app.state.session_factory = create_session_factory(engine)
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder(settings.embedding_dimension)
    app.dependency_overrides[get_chatter] = lambda: FakeChatter()
    return app


@pytest.fixture
def tenant_with_key(app_with_database: FastAPI) -> str:
    """Create a tenant with one active API key, returning the raw key.

    Most tests only care that a valid key exists, not which tenant it
    belongs to - the tenant and its api_keys row are plumbing that
    get_tenant needs, not something these tests inspect.
    """

    async def create() -> str:
        key = generate_api_key()
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        async with session_factory() as session:
            tenant = Tenant(name="test-tenant")
            session.add(tenant)
            await session.flush()
            session.add(
                ApiKey(
                    tenant_id=tenant.id,
                    key_hash=hash_api_key(key),
                    prefix=api_key_prefix(key),
                    name="test-key",
                )
            )
            await session.commit()
        return key

    return asyncio.run(create())


@pytest.fixture
def app_with_unreachable_database() -> FastAPI:
    """A FastAPI app wired to a database that refuses connections.

    Points at a closed local port. Connecting to a port nothing listens
    on fails immediately with "connection refused" - no network timeout
    to wait out.
    """
    app = create_app()
    unreachable = Settings(
        database_url="postgresql+psycopg://ragbridge:ragbridge@localhost:1/ragbridge"
    )
    engine = create_engine(unreachable)
    app.state.session_factory = create_session_factory(engine)
    return app
