"""Application entry point: builds the FastAPI app."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI

from ragbridge import tracing
from ragbridge.api.agent import router as agent_router
from ragbridge.api.documents import router as documents_router
from ragbridge.api.health import router as health_router
from ragbridge.api.query import router as query_router
from ragbridge.api.search import router as search_router
from ragbridge.config import get_settings
from ragbridge.db.session import create_engine, create_session_factory
from ragbridge.mcp_server.http import mount_mcp
from ragbridge.playground import mount_playground


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Create the database engine on startup, dispose it on shutdown.

    Also runs the MCP session manager for the app's lifetime: the ``/mcp``
    transport is a mounted ASGI app, and mounted apps get no lifespan of
    their own.

    The engine is created here, not at import time, so importing the app
    module never opens a network connection.
    """
    settings = get_settings()
    engine = create_engine(settings)
    app.state.session_factory = create_session_factory(engine)
    async with app.state.mcp_server.session_manager.run():
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
    # Passing None for both removes the routes entirely, rather than
    # serving a docs page that cannot load its own schema.
    app = FastAPI(
        title=settings.app_name,
        version=version("ragbridge"),
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
    )
    app.include_router(health_router)
    app.include_router(documents_router)
    app.include_router(query_router)
    app.include_router(agent_router)
    app.include_router(search_router)
    app.state.mcp_server = mount_mcp(app)
    if settings.enable_playground:
        mount_playground(app)
    return app


app = create_app()
