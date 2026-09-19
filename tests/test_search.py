"""Tests for POST /search."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.auth import generate_api_key
from ragbridge.rerank import FakeReranker, get_reranker

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
    assert set(response.json()) == {"results"}
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


ERROR_CHUNK = "Error code ERR_4021 means the upload exceeded the size limit."
OTHER_CHUNK = "Cats are independent and curious animals."


def _upload_two_chunks(client: TestClient) -> None:
    client.post("/documents", files={"file": ("errors.txt", ERROR_CHUNK.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", OTHER_CHUNK.encode(), "text/plain")})


def test_search_without_explain_has_no_retrieval_fields(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    explained = client.post("/search", json={"query": ERROR_CHUNK, "explain": False}).json()
    plain = client.post("/search", json={"query": ERROR_CHUNK}).json()

    assert explained == plain
    assert "candidate_count" not in plain
    assert all("retrieval" not in hit for hit in plain["results"])


def test_search_explain_reports_which_arm_found_each_chunk(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The exact chunk text is rank 1 in both arms (FakeEmbedder gives equal
    text an equal vector), so its fused score is 1/61 + 1/61. The cats chunk
    has no term in common with the query, so only the vector arm found it.
    """
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    body = client.post("/search", json={"query": ERROR_CHUNK, "explain": True}).json()

    error_hit, cats_hit = body["results"]
    assert error_hit["content"] == ERROR_CHUNK
    assert error_hit["retrieval"] == {
        "vector_rank": 1,
        "keyword_rank": 1,
        "fused_score": pytest.approx(1 / 61 + 1 / 61),
        "rank_before_rerank": 1,
    }
    assert cats_hit["retrieval"]["keyword_rank"] is None
    assert cats_hit["retrieval"]["vector_rank"] == 2
    assert body["candidate_count"] == 2


def test_search_explain_shows_what_reranking_changed(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """FakeReranker reverses the order, so the first hit was second before it."""
    app_with_database.dependency_overrides[get_reranker] = lambda: FakeReranker()
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    body = client.post("/search", json={"query": ERROR_CHUNK, "explain": True}).json()

    assert [hit["content"] for hit in body["results"]] == [OTHER_CHUNK, ERROR_CHUNK]
    assert [hit["retrieval"]["rank_before_rerank"] for hit in body["results"]] == [2, 1]


def test_search_explain_counts_candidates_beyond_top_k(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    body = client.post("/search", json={"query": ERROR_CHUNK, "top_k": 1, "explain": True}).json()

    assert len(body["results"]) == 1
    assert body["candidate_count"] == 2


def test_search_explain_does_not_leak_another_tenants_chunks(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    _upload_two_chunks(_client(app_with_database, tenant_with_key))

    body = (
        _client(app_with_database, second_tenant_with_key)
        .post("/search", json={"query": ERROR_CHUNK, "explain": True})
        .json()
    )

    assert body == {"results": [], "candidate_count": 0}
