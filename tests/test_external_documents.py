"""Tests for the endpoints that identify a document by the client's own id."""

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document, Tenant
from tests.helpers import fetch_chunks


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


RECORD = {
    "title": "Refund policy",
    "content": "Refunds are possible within 14 days of purchase.",
    "metadata": {"lang": "en", "post_id": 42},
    "source_updated_at": "2026-09-21T10:00:00Z",
}


def _put(client: TestClient, external_id: str = "post:42", **changes: Any) -> Response:
    return client.put(f"/documents/external/{external_id}", json={**RECORD, **changes})


def test_put_creates_a_document_under_the_external_id(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = _put(client)

    assert response.status_code == 201
    body = response.json()
    assert body["result"] == "created"
    document = body["document"]
    assert document["external_id"] == "post:42"
    assert document["filename"] == "Refund policy"
    assert document["content_type"] == "text/plain"
    assert document["status"] == "ready"
    assert document["metadata"] == {"lang": "en", "post_id": 42}
    assert document["source_updated_at"] == "2026-09-21T10:00:00Z"
    assert client.get("/documents/external/post:42").json() == document


def test_put_chunks_and_embeds_the_content(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    document_id = uuid.UUID(
        _put(_client(app_with_database, tenant_with_key)).json()["document"]["id"]
    )

    chunks = asyncio.run(fetch_chunks(app_with_database.state.session_factory, document_id))

    assert [chunk.content for chunk in chunks] == [RECORD["content"]]


def test_put_without_optional_fields_stores_an_empty_object_and_no_timestamp(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = client.put("/documents/external/post:1", json={"title": "T", "content": "Text."})

    assert response.status_code == 201
    assert response.json()["document"]["metadata"] == {}
    assert response.json()["document"]["source_updated_at"] is None


def test_two_records_with_identical_text_are_two_documents(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    first = _put(client, "post:1")
    second = _put(client, "post:2")

    assert first.status_code == second.status_code == 201
    assert first.json()["document"]["id"] != second.json()["document"]["id"]
    assert len(client.get("/documents").json()) == 2


def test_the_same_id_in_two_tenants_creates_two_documents(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    first = _put(_client(app_with_database, tenant_with_key))
    second = _put(_client(app_with_database, second_tenant_with_key))

    assert first.status_code == second.status_code == 201
    assert first.json()["document"]["id"] != second.json()["document"]["id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "x" * 501},
        {"content": ""},
        {"content": "   \n\t "},
        {"metadata": ["not", "an", "object"]},
        {"metadata": {"blob": "x" * 20_000}},
        {"source_updated_at": "2026-09-21T10:00:00"},  # no time zone
        {"source_updated_at": "yesterday"},
    ],
)
def test_put_rejects_an_invalid_body(
    app_with_database: FastAPI, tenant_with_key: str, changes: dict[str, Any]
) -> None:
    response = _put(_client(app_with_database, tenant_with_key), **changes)

    assert response.status_code == 422


def test_put_needs_a_title_and_content(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = _client(app_with_database, tenant_with_key)

    assert client.put("/documents/external/a", json={"content": "x"}).status_code == 422
    assert client.put("/documents/external/a", json={"title": "x"}).status_code == 422


def test_put_rejects_an_invalid_external_id(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _put(_client(app_with_database, tenant_with_key), ".hidden")

    assert response.status_code == 422


def test_put_rejects_content_over_the_upload_limit(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(max_upload_size=10)

    response = _put(_client(app_with_database, tenant_with_key))

    assert response.status_code == 413


def test_the_upload_limit_counts_utf8_bytes_not_characters(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(max_upload_size=10)

    response = _put(_client(app_with_database, tenant_with_key), content="é" * 8)  # 16 bytes

    assert response.status_code == 413


def test_put_needs_authentication(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).put("/documents/external/a", json=RECORD)

    assert response.status_code == 401


def test_a_synced_document_is_listed_and_deletable_by_uuid(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document_id = _put(client).json()["document"]["id"]

    assert [d["id"] for d in client.get("/documents").json()] == [document_id]
    assert client.delete(f"/documents/{document_id}").status_code == 204
    assert client.get("/documents/external/post:42").status_code == 404
