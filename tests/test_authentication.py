"""Tests that every documents/query endpoint requires a valid API key.

Missing, malformed, unknown, and revoked keys all get the same 401 -
proven here as a behavioural contract, not just as a property of
get_tenant's implementation (see tests/test_auth.py for that).
"""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from ragbridge.auth import generate_api_key
from ragbridge.db.models import ApiKey


def test_request_with_no_authorization_header_is_rejected(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)

    response = client.get("/documents")

    assert response.status_code == 401


def test_request_with_a_malformed_header_is_rejected(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database, headers={"Authorization": "not-a-bearer-token"})

    response = client.get("/documents")

    assert response.status_code == 401


def test_request_with_an_unknown_key_is_rejected(app_with_database: FastAPI) -> None:
    unknown_key = generate_api_key()
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {unknown_key}"})

    response = client.get("/documents")

    assert response.status_code == 401


def test_request_with_a_revoked_key_is_rejected(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def revoke() -> None:
        async with session_factory() as session:
            await session.execute(update(ApiKey).values(revoked_at=func.now()))
            await session.commit()

    asyncio.run(revoke())
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.get("/documents")

    assert response.status_code == 401


def test_request_with_a_valid_key_succeeds(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.get("/documents")

    assert response.status_code == 200


def test_health_requires_no_authorization_header(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)

    response = client.get("/health")

    assert response.status_code == 200
