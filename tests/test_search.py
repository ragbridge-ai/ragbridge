"""Tests for POST /search."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.auth import generate_api_key

LONG_TEXT = "The mitochondria is the powerhouse of the cell. " * 12


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_search_returns_the_whole_chunk_not_a_snippet(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    client.post("/documents", files={"file": ("a.txt", LONG_TEXT.encode(), "text/plain")})

    response = client.post("/search", json={"query": "mitochondria"})

    assert response.status_code == 200
    [hit] = response.json()["results"]
    assert set(hit) == {"document_id", "filename", "chunk_index", "content", "score"}
    assert hit["filename"] == "a.txt"
    assert hit["content"] == LONG_TEXT.strip()
    assert len(hit["content"]) > 300


def test_search_respects_top_k(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = _client(app_with_database, tenant_with_key)
    for name, text in [("a.txt", b"Refunds take five days."), ("b.txt", b"Refunds are partial.")]:
        client.post("/documents", files={"file": (name, text, "text/plain")})

    response = client.post("/search", json={"query": "refunds", "top_k": 1})

    assert len(response.json()["results"]) == 1


def test_search_rejects_a_top_k_above_the_limit(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).post(
        "/search", json={"query": "q", "top_k": 21}
    )

    assert response.status_code == 422


def test_search_with_no_authorization_header_is_rejected(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).post("/search", json={"query": "q"})

    assert response.status_code == 401


def test_search_with_an_unknown_key_is_rejected(app_with_database: FastAPI) -> None:
    response = _client(app_with_database, generate_api_key()).post("/search", json={"query": "q"})

    assert response.status_code == 401


def test_search_never_returns_another_tenants_chunks(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    secret = "The launch code is 8842."
    _client(app_with_database, tenant_with_key).post(
        "/documents", files={"file": ("a.txt", secret.encode(), "text/plain")}
    )

    response = _client(app_with_database, second_tenant_with_key).post(
        "/search", json={"query": secret}
    )

    assert response.json()["results"] == []
