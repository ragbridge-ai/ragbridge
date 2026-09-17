"""Application entry point: builds the FastAPI app."""

from fastapi import FastAPI

from ragbridge.api.health import router as health_router
from ragbridge.config import get_settings


def create_app() -> FastAPI:
    """Build and configure a FastAPI application instance.

    Using a factory (instead of a module-level app built at import time)
    keeps app creation explicit and lets tests build a fresh, isolated
    app for each test run.
    """
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.include_router(health_router)
    return app


app = create_app()
