"""Tests for the ENABLE_DOCS switch.

``enable_docs`` is read when the app is *built*, not per request, so
``dependency_overrides`` cannot reach it - these set the environment and
rebuild the app, which is the real path.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ragbridge.config import get_settings
from ragbridge.main import create_app

DOC_PATHS = ("/docs", "/openapi.json", "/redoc")


@pytest.fixture
def rebuilt_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Let a test set settings in the environment and build a fresh app.

    ``get_settings`` is ``lru_cache``d, so the cache is cleared on the way
    in and again on the way out - otherwise one test's settings would
    leak into every later one.
    """
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_docs_are_served_by_default(rebuilt_app: pytest.MonkeyPatch) -> None:
    rebuilt_app.delenv("ENABLE_DOCS", raising=False)
    client = TestClient(create_app())

    for path in DOC_PATHS:
        assert client.get(path).status_code == 200, path


def test_docs_are_absent_when_disabled(rebuilt_app: pytest.MonkeyPatch) -> None:
    rebuilt_app.setenv("ENABLE_DOCS", "false")
    client = TestClient(create_app())

    for path in DOC_PATHS:
        assert client.get(path).status_code == 404, path


def test_the_api_still_works_when_docs_are_disabled(rebuilt_app: pytest.MonkeyPatch) -> None:
    """Turning the docs off must not touch the application itself."""
    rebuilt_app.setenv("ENABLE_DOCS", "false")
    client = TestClient(create_app())

    assert client.get("/health").status_code == 200
    # Still authenticated, rather than accidentally opened up. /mcp is
    # checked because it rejects before touching the database, which
    # this file deliberately does not set up.
    assert client.post("/mcp", json={}).status_code == 401
