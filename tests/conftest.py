"""Shared fixtures for tests that need a database-backed app."""

import pytest
from fastapi import FastAPI

from ragbridge.config import Settings
from ragbridge.db.session import create_engine, create_session_factory
from ragbridge.main import create_app


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
