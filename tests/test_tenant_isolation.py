"""Proves one tenant's data is invisible to another.

This is the safety net decision 3 (docs/plans/phase-3.md) calls for in
place of Postgres Row-Level Security: application-level tenant
filtering has no database-enforced backstop, so a forgotten WHERE
clause must fail loudly here, the same day it is introduced, rather
than leak silently.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_tenant_b_does_not_see_tenant_as_document_in_the_list(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    client_a = _client(app_with_database, tenant_with_key)
    client_b = _client(app_with_database, second_tenant_with_key)
    client_a.post("/documents", files={"file": ("a.txt", b"tenant a's content", "text/plain")})
    client_b.post("/documents", files={"file": ("b.txt", b"tenant b's content", "text/plain")})

    response = client_b.get("/documents")

    assert [document["filename"] for document in response.json()] == ["b.txt"]


def test_tenant_b_gets_404_deleting_tenant_as_document(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    client_a = _client(app_with_database, tenant_with_key)
    client_b = _client(app_with_database, second_tenant_with_key)
    uploaded = client_a.post(
        "/documents", files={"file": ("a.txt", b"tenant a's content", "text/plain")}
    )
    document_id = uploaded.json()["id"]

    response = client_b.delete(f"/documents/{document_id}")

    assert response.status_code == 404
    assert client_a.get("/documents").json() != []


def test_tenant_b_query_never_returns_tenant_as_content(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    client_a = _client(app_with_database, tenant_with_key)
    client_b = _client(app_with_database, second_tenant_with_key)
    secret = "The launch code is 8842."
    client_a.post("/documents", files={"file": ("a.txt", secret.encode(), "text/plain")})

    response = client_b.post("/query", json={"question": secret})

    assert response.json()["sources"] == []


def test_identical_content_uploaded_by_two_tenants_creates_two_documents(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    """sha256 is unique per tenant, not globally (decision 3): the second
    tenant uploading identical bytes must get its own document, never
    silently read back the first tenant's.
    """
    client_a = _client(app_with_database, tenant_with_key)
    client_b = _client(app_with_database, second_tenant_with_key)
    content = b"Identical bytes, uploaded by two different tenants."

    response_a = client_a.post("/documents", files={"file": ("shared.txt", content, "text/plain")})
    response_b = client_b.post("/documents", files={"file": ("shared.txt", content, "text/plain")})

    assert response_a.status_code == 201
    assert response_b.status_code == 201
    assert response_a.json()["id"] != response_b.json()["id"]
