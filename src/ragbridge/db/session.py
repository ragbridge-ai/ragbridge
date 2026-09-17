"""Async database engine and per-request session handling.

Nothing here runs at import time. The engine is created in the app's
lifespan (see ``ragbridge.main``), so importing this module - or building
an app for tests - never opens a network connection.
"""

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ragbridge.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build an async engine. Call once, at app startup."""
    return create_async_engine(settings.database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to the given engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a session for one request.

    The session factory lives on ``app.state`` (set up in the lifespan),
    not as a module-level global, so each app instance - including test
    apps - gets its own engine and sessions.
    """
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session
