"""Tests for API keys marked ``requires_access_scope``.

Such a key must name its access groups on every retrieval request, and cannot manage
documents at all (decision 3, docs/plans/chat-roadmap.md). Plain keys are unchanged.
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from ragbridge.db.models import ApiKey
from tests.helpers import add_document, create_api_key, tenant_id_for_key

PUBLIC_PATHS = {"/health", "/health/ready"}
FILE = ("a.txt", b"Refunds take five days.", "text/plain")


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


@pytest.fixture
def scoped_key(app_with_database: FastAPI, tenant_with_key: str) -> str:
    """A key marked ``requires_access_scope``, for the same tenant as ``tenant_with_key``."""
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def make() -> str:
        tenant_id = await tenant_id_for_key(session_factory, tenant_with_key)
        return await create_api_key(session_factory, tenant_id, requires_access_scope=True)

    return asyncio.run(make())


def _seed(app: FastAPI, key: str) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory

    async def run() -> None:
        tenant_id = await tenant_id_for_key(session_factory, key)
        async with session_factory() as session:
            await add_document(
                session, tenant_id, "public", ["Refunds take five days."], ["public"]
            )
            await add_document(session, tenant_id, "hr", ["Salaries are reviewed."], ["hr"])
            await session.commit()

    asyncio.run(run())


RETRIEVAL = {
    "/search": {"query": "refunds salaries"},
    "/query": {"question": "refunds salaries"},
    "/agent": {"question": "refunds salaries"},
}


@pytest.mark.parametrize("endpoint", RETRIEVAL)
@pytest.mark.parametrize("extra", [{}, {"access_groups": []}], ids=["missing", "empty"])
def test_a_scoped_key_without_groups_is_refused_on_each_retrieval_endpoint(
    app_with_database: FastAPI, scoped_key: str, endpoint: str, extra: dict[str, Any]
) -> None:
    response = _client(app_with_database, scoped_key).post(
        endpoint, json={**RETRIEVAL[endpoint], **extra}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "this API key requires access_groups"}


@pytest.mark.parametrize("endpoint", RETRIEVAL)
def test_a_scoped_key_with_groups_is_filtered_like_any_other_key(
    app_with_database: FastAPI, tenant_with_key: str, scoped_key: str, endpoint: str
) -> None:
    _seed(app_with_database, tenant_with_key)

    response = _client(app_with_database, scoped_key).post(
        endpoint, json={**RETRIEVAL[endpoint], "access_groups": ["public"]}
    )

    assert response.status_code == 200
    items = response.json()["results" if endpoint == "/search" else "sources"]
    assert {item["filename"] for item in items} == {"public"}


@pytest.mark.parametrize("endpoint", RETRIEVAL)
def test_a_plain_key_is_unchanged_on_the_retrieval_endpoints(
    app_with_database: FastAPI, tenant_with_key: str, endpoint: str
) -> None:
    _seed(app_with_database, tenant_with_key)

    response = _client(app_with_database, tenant_with_key).post(endpoint, json=RETRIEVAL[endpoint])

    assert response.status_code == 200
    items = response.json()["results" if endpoint == "/search" else "sources"]
    assert {item["filename"] for item in items} == {"public", "hr"}


DOCUMENT_ID = str(uuid.uuid4())
DOCUMENT_CALLS: dict[str, Callable[[TestClient], Response]] = {
    "upload": lambda c: c.post("/documents", files={"file": FILE}),
    "list": lambda c: c.get("/documents"),
    "get": lambda c: c.get(f"/documents/{DOCUMENT_ID}"),
    "patch": lambda c: c.patch(f"/documents/{DOCUMENT_ID}", json={"access_groups": ["hr"]}),
    "delete": lambda c: c.delete(f"/documents/{DOCUMENT_ID}"),
    "get external": lambda c: c.get("/documents/external/post:1"),
    "put external": lambda c: c.put(
        "/documents/external/post:1", json={"title": "post", "content": "Some text."}
    ),
    "delete external": lambda c: c.delete("/documents/external/post:1"),
}


@pytest.mark.parametrize("call", DOCUMENT_CALLS)
def test_a_scoped_key_cannot_use_any_documents_endpoint(
    app_with_database: FastAPI, tenant_with_key: str, scoped_key: str, call: str
) -> None:
    response = DOCUMENT_CALLS[call](_client(app_with_database, scoped_key))

    assert response.status_code == 403
    assert response.json() == {"detail": "this API key cannot manage documents"}
    assert _client(app_with_database, tenant_with_key).get("/documents").json() == []


@pytest.mark.parametrize("call", ["upload", "list", "put external"])
def test_a_plain_key_still_uses_the_documents_endpoints(
    app_with_database: FastAPI, tenant_with_key: str, call: str
) -> None:
    response = DOCUMENT_CALLS[call](_client(app_with_database, tenant_with_key))

    assert response.status_code in {200, 201}


def test_an_unknown_or_revoked_key_is_still_a_401_not_a_403(
    app_with_database: FastAPI, scoped_key: str
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def revoke() -> None:
        async with session_factory() as session:
            await session.execute(update(ApiKey).values(revoked_at=func.now()))
            await session.commit()

    unknown = _client(app_with_database, "rb_unknown")
    asyncio.run(revoke())
    revoked = _client(app_with_database, scoped_key)

    for client in (unknown, revoked):
        assert client.get("/documents").status_code == 401
        assert client.post("/search", json={"query": "x"}).status_code == 401


def test_every_endpoint_but_the_health_checks_turns_a_scoped_key_away(
    app_with_database: FastAPI, scoped_key: str
) -> None:
    """The guard behind "closed by default": an endpoint added later that forgets the check
    fails here. It calls each operation in the app's OpenAPI schema with a scoped key and no
    groups.

    The `/documents` endpoints are turned away by authentication, before their request is
    read, so they are sent nothing. The retrieval endpoints check the groups inside the
    endpoint, so they need a valid body to get that far. A new endpoint that takes a body
    answers 422 here until it is added to ``RETRIEVAL``, which is the failure that tells
    its author to decide what a scoped key may do with it.
    """
    client = _client(app_with_database, scoped_key)
    operations = [
        (method, path)
        for path, methods in app_with_database.openapi()["paths"].items()
        if path not in PUBLIC_PATHS
        for method in methods
    ]
    assert len(operations) >= 11  # 8 on /documents and 3 retrieval endpoints today

    statuses = {
        (method, path): client.request(
            method.upper(),
            path.replace("{document_id}", DOCUMENT_ID).replace("{external_id}", "post:1"),
            json=RETRIEVAL.get(path),
        ).status_code
        for method, path in operations
    }

    assert {key: code for key, code in statuses.items() if code != 403} == {}
