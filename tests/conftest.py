"""Shared fixtures for tests that need a database-backed app."""

import asyncio

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from ragbridge.config import Settings
from ragbridge.db.session import create_engine, create_session_factory
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
            await connection.execute(text("TRUNCATE TABLE documents"))
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
    engine = create_engine(Settings())
    app.state.session_factory = create_session_factory(engine)
    return app


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
