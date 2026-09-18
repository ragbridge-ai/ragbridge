"""Tests for POST /documents, GET /documents, and DELETE /documents/{id}."""

import asyncio
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.config import Settings, get_settings
from tests.helpers import build_pdf, fetch_chunks


def test_upload_document_creates_a_new_document(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    content = b"Hello, ragbridge."

    response = client.post("/documents", files={"file": ("hello.txt", content, "text/plain")})

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "hello.txt"
    assert body["content_type"] == "text/plain"
    assert "id" in body
    assert "created_at" in body


def test_upload_document_twice_returns_the_existing_document(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    content = b"Same content, uploaded twice."

    first = client.post("/documents", files={"file": ("a.txt", content, "text/plain")})
    second = client.post("/documents", files={"file": ("b.md", content, "text/markdown")})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_upload_document_rejects_oversized_file(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(max_upload_size=5)
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.post(
        "/documents", files={"file": ("big.txt", b"more than five bytes", "text/plain")}
    )

    assert response.status_code == 413


def test_upload_document_rejects_unsupported_content_type(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.post("/documents", files={"file": ("data.json", b"{}", "application/json")})

    assert response.status_code == 415


def test_list_documents_returns_newest_first(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    first = client.post("/documents", files={"file": ("first.txt", b"first", "text/plain")})
    second = client.post("/documents", files={"file": ("second.txt", b"second", "text/plain")})

    response = client.get("/documents")

    assert response.status_code == 200
    ids = [document["id"] for document in response.json()]
    assert ids == [second.json()["id"], first.json()["id"]]


def test_list_documents_returns_empty_list_when_there_are_none(
    app_with_database: FastAPI,
    tenant_with_key: str,
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.get("/documents")

    assert response.status_code == 200
    assert response.json() == []


def test_delete_document_removes_it(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    uploaded = client.post("/documents", files={"file": ("a.txt", b"content", "text/plain")})
    document_id = uploaded.json()["id"]

    response = client.delete(f"/documents/{document_id}")

    assert response.status_code == 204
    assert client.get("/documents").json() == []


def test_delete_document_returns_404_when_missing(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.delete(f"/documents/{uuid.uuid4()}")

    assert response.status_code == 404


def test_upload_text_document_creates_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    content = b"First paragraph.\n\nSecond paragraph."

    response = client.post("/documents", files={"file": ("notes.txt", content, "text/plain")})

    document_id = uuid.UUID(response.json()["id"])
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    chunks = asyncio.run(fetch_chunks(session_factory, document_id))

    assert [chunk.content for chunk in chunks] == ["First paragraph.", "Second paragraph."]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert all(chunk.metadata_ == {} for chunk in chunks)
    assert all(len(chunk.embedding) == get_settings().embedding_dimension for chunk in chunks)


def test_upload_pdf_document_creates_chunks_with_page_metadata(
    app_with_database: FastAPI,
    tenant_with_key: str,
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    pdf_bytes = build_pdf(["Page one text.", "Page two text."])

    response = client.post("/documents", files={"file": ("doc.pdf", pdf_bytes, "application/pdf")})

    assert response.status_code == 201
    document_id = uuid.UUID(response.json()["id"])
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    chunks = asyncio.run(fetch_chunks(session_factory, document_id))

    assert [chunk.content for chunk in chunks] == ["Page one text.", "Page two text."]
    assert [chunk.metadata_ for chunk in chunks] == [{"page": 1}, {"page": 2}]


def test_upload_document_twice_does_not_duplicate_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    content = b"Same content, uploaded twice."

    first = client.post("/documents", files={"file": ("a.txt", content, "text/plain")})
    client.post("/documents", files={"file": ("b.txt", content, "text/plain")})

    document_id = uuid.UUID(first.json()["id"])
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    chunks = asyncio.run(fetch_chunks(session_factory, document_id))

    assert len(chunks) == 1


def test_delete_document_deletes_its_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    uploaded = client.post("/documents", files={"file": ("a.txt", b"some content", "text/plain")})
    document_id = uuid.UUID(uploaded.json()["id"])

    client.delete(f"/documents/{document_id}")

    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    chunks = asyncio.run(fetch_chunks(session_factory, document_id))

    assert chunks == []
