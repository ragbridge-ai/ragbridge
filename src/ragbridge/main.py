"""Application entry point: builds the FastAPI app."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ragbridge import tracing
from ragbridge.api.agent import router as agent_router
from ragbridge.api.documents import router as documents_router
from ragbridge.api.health import router as health_router
from ragbridge.api.query import router as query_router
from ragbridge.api.search import router as search_router
from ragbridge.config import get_settings
from ragbridge.db.session import create_engine, create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Create the database engine on startup, dispose it on shutdown.

    The engine is created here, not at import time, so importing the app
    module never opens a network connection.
    """
    settings = get_settings()
    engine = create_engine(settings)
    app.state.session_factory = create_session_factory(engine)
    yield
    await engine.dispose()
    tracing.flush(settings)


def create_app() -> FastAPI:
    """Build and configure a FastAPI application instance.

    Using a factory (instead of a module-level app built at import time)
    keeps app creation explicit and lets tests build a fresh, isolated
    app for each test run.
    """
    settings = get_settings()
    tracing.configure(settings)
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(documents_router)
    app.include_router(query_router)
    app.include_router(agent_router)
    app.include_router(search_router)
    return app


app = create_app()
