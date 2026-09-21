"""Tests for the endpoints that identify a document by the client's own id."""

import asyncio
import hashlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.db.models import Document, Tenant


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


async def _insert_synced_document(
    app: FastAPI, tenant_name: str, external_id: str, content: str = "Some text."
) -> uuid.UUID:
    """Insert a synced document directly, until PUT exists to create one."""
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.name == tenant_name))
        ).scalar_one()
        document = Document(
            tenant_id=tenant_id,
            external_id=external_id,
            filename="A title",
            content_type="text/plain",
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
            metadata_={"lang": "en"},
        )
        session.add(document)
        await session.commit()
        return document.id


def test_get_by_external_id_returns_the_document(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    document_id = asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "post:42"))

    response = _client(app_with_database, tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(document_id)
    assert body["external_id"] == "post:42"
    assert body["filename"] == "A title"
    assert body["metadata"] == {"lang": "en"}


def test_get_by_external_id_is_404_when_there_is_none(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 404


def test_another_tenants_external_id_is_404(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "post:42"))

    response = _client(app_with_database, second_tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 404


def test_the_same_external_id_in_two_tenants_is_two_documents(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    first = asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "post:1"))
    second = asyncio.run(_insert_synced_document(app_with_database, "test-tenant-2", "post:1"))

    one = _client(app_with_database, tenant_with_key).get("/documents/external/post:1")
    two = _client(app_with_database, second_tenant_with_key).get("/documents/external/post:1")

    assert one.json()["id"] == str(first)
    assert two.json()["id"] == str(second)


def test_the_external_id_is_case_sensitive(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "Post:42"))

    response = _client(app_with_database, tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "external_id",
    [
        "post:42",
        "post%3A42",
        "user@example.com",
        "shop-de:product:9f1c2b3a",
        "wp_posts.42",
        "7",
        "a" * 255,
    ],
)
def test_valid_external_ids_reach_the_handler(
    app_with_database: FastAPI, tenant_with_key: str, external_id: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get(f"/documents/external/{external_id}")

    assert response.status_code == 404  # valid id, no such document


@pytest.mark.parametrize(
    "external_id",
    [
        "a" * 256,  # too long
        "%2E%2E",  # decodes to ".."
        ".hidden",  # must start with a letter or digit
        "-dash",
        "_under",
        "caf%C3%A9",  # not ASCII
        "a%20b",  # a space
        "a%2Fb",  # an encoded slash never reaches the id (see below)
        "a%3Fb",  # "?"
        "a%23b",  # "#"
    ],
)
def test_invalid_external_ids_are_rejected(
    app_with_database: FastAPI, tenant_with_key: str, external_id: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get(f"/documents/external/{external_id}")

    assert response.status_code in {404, 422}
    assert response.status_code != 200


def test_an_id_outside_the_pattern_is_422_and_names_the_rule(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get("/documents/external/.hidden")

    assert response.status_code == 422
    assert "pattern" in response.text


def test_get_by_external_id_needs_authentication(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).get("/documents/external/post:42")

    assert response.status_code == 401


def test_an_upload_still_works_beside_the_external_routes(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    created = client.post("/documents", files={"file": ("a.txt", b"Plain.", "text/plain")})

    assert client.get(f"/documents/{created.json()['id']}").status_code == 200
