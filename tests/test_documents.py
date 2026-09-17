"""Tests for POST /documents."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.config import Settings, get_settings


def test_upload_document_creates_a_new_document(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    content = b"Hello, ragbridge."

    response = client.post("/documents", files={"file": ("hello.txt", content, "text/plain")})

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "hello.txt"
    assert body["content_type"] == "text/plain"
    assert "id" in body
    assert "created_at" in body


def test_upload_document_twice_returns_the_existing_document(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    content = b"Same content, uploaded twice."

    first = client.post("/documents", files={"file": ("a.txt", content, "text/plain")})
    second = client.post("/documents", files={"file": ("b.md", content, "text/markdown")})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_upload_document_rejects_oversized_file(app_with_database: FastAPI) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(max_upload_size=5)
    client = TestClient(app_with_database)

    response = client.post(
        "/documents", files={"file": ("big.txt", b"more than five bytes", "text/plain")}
    )

    assert response.status_code == 413


def test_upload_document_rejects_unsupported_content_type(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)

    response = client.post("/documents", files={"file": ("data.json", b"{}", "application/json")})

    assert response.status_code == 415
