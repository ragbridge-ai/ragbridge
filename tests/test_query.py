"""Tests for POST /query."""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_query_ranks_the_exact_matching_chunk_first(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    exact_match = "The mitochondria is the powerhouse of the cell."
    client.post("/documents", files={"file": ("a.txt", exact_match.encode(), "text/plain")})
    other = "Paris is the capital of France."
    client.post("/documents", files={"file": ("b.txt", other.encode(), "text/plain")})

    response = client.post("/query", json={"question": exact_match, "top_k": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["snippet"] == exact_match
    assert body["sources"][0]["score"] == 1.0
    assert body["answer"] == "Fake answer using 2 chunk(s)."


def test_query_limits_sources_to_top_k(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    for i in range(3):
        content = f"Fact number {i}.".encode()
        client.post("/documents", files={"file": (f"{i}.txt", content, "text/plain")})

    response = client.post("/query", json={"question": "Fact number 1.", "top_k": 2})

    assert response.status_code == 200
    assert len(response.json()["sources"]) == 2


def test_query_with_no_documents_returns_no_sources(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)

    response = client.post("/query", json={"question": "Anything?"})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"] == []
    assert body["answer"] == "Fake answer using 0 chunk(s)."


def test_query_source_fields(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    content = "Some fact about ragbridge."
    files = {"file": ("notes.txt", content.encode(), "text/plain")}
    upload = client.post("/documents", files=files)
    document_id = upload.json()["id"]

    response = client.post("/query", json={"question": content})

    source = response.json()["sources"][0]
    assert source["document_id"] == document_id
    assert source["filename"] == "notes.txt"
    assert source["chunk_index"] == 0
    assert source["snippet"] == content


def test_query_rejects_top_k_above_the_maximum(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)

    response = client.post("/query", json={"question": "Anything?", "top_k": 21})

    assert response.status_code == 422
