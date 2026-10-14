"""Proves a request scoped to some access groups never sees a document outside them.

The counterpart of ``test_tenant_isolation.py`` for step 7.1 of docs/plans/chat-roadmap.md,
and the safety net for choosing a filter in application code over one the database
enforces: a forgotten group condition in any read must fail here the day it is introduced,
not leak later.

Everything goes through the real endpoints, with real chunking, so the documents are
written the way a caller writes them. Each document's text carries a marker word that
appears nowhere else, so a leak is visible in the returned text and not only in a filename.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.config import Settings, get_settings

# filename -> groups. "nobody" has none and must never appear for any list of groups.
DOCUMENTS: dict[str, list[str]] = {
    "public": ["public"],
    "hr": ["hr"],
    "engineering": ["engineering"],
    "hr-and-engineering": ["hr", "engineering"],
    "nobody": [],
}
SCOPES: list[list[str] | None] = [
    ["public"],
    ["hr"],
    ["engineering"],
    ["hr", "engineering"],
    ["hr", "engineering", "public"],
    ["unknown"],
    [],
    None,
]
ENDPOINTS = ["/search", "/query", "/query (answer cache on)", "/agent"]


def _marker(name: str) -> str:
    return f"zz{name.replace('-', '')}zz"


def _text(name: str) -> bytes:
    """Three paragraphs, each long enough to stay a chunk of its own, so there are
    neighbouring chunks for the answer's context to reach for."""
    paragraphs = [
        f"Policy block {n} of {name}: {_marker(name)} is written here, in a paragraph long "
        "enough that it stays a chunk of its own instead of being merged into the one before it."
        for n in range(3)
    ]
    return "\n\n".join(paragraphs).encode()


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _upload_all(client: TestClient) -> None:
    for name, groups in DOCUMENTS.items():
        data = {"access_groups": groups} if groups else None
        response = client.post(
            "/documents", files={"file": (name, _text(name), "text/plain")}, data=data
        )
        assert response.status_code == 201, response.text


def _expected(scope: list[str] | None) -> set[str]:
    if scope is None:
        return set(DOCUMENTS)
    return {name for name, groups in DOCUMENTS.items() if set(groups) & set(scope)}


def _call(client: TestClient, endpoint: str, scope: list[str] | None) -> tuple[set[str], str]:
    """The documents an endpoint returns, and all the text it returns, for ``scope``."""
    field = "query" if endpoint == "/search" else "question"
    body: dict[str, Any] = {field: "policy block", "top_k": 20}
    if endpoint == "/agent":
        del body["top_k"]
    if scope is not None:
        body["access_groups"] = scope
    response = client.post(endpoint.split(" ")[0], json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    if endpoint == "/search":
        items = payload["results"]
        text = " ".join(item["content"] for item in items)
    else:
        items = payload["sources"]
        text = " ".join(item["snippet"] for item in items)
    return {item["filename"] for item in items}, text


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("scope", SCOPES, ids=lambda scope: "none" if scope is None else str(scope))
def test_a_scoped_request_sees_only_documents_sharing_one_of_its_groups(
    app_with_database: FastAPI,
    tenant_with_key: str,
    second_tenant_with_key: str,
    endpoint: str,
    scope: list[str] | None,
) -> None:
    if endpoint == "/query (answer cache on)":
        app_with_database.dependency_overrides[get_settings] = lambda: Settings(
            answer_cache_enabled=True
        )
    client = _client(app_with_database, tenant_with_key)
    _upload_all(client)
    # Another tenant's document, labelled with the same group names: groups never cross
    # a tenant.
    other = _client(app_with_database, second_tenant_with_key)
    other.post(
        "/documents",
        files={"file": ("other-tenant", _text("othertenant"), "text/plain")},
        data={"access_groups": ["hr", "public", "engineering"]},
    )

    found, text = _call(client, endpoint, scope)
    # Asked twice: with the answer cache on, the second call is the one that is served
    # from it.
    found_again, text_again = _call(client, endpoint, scope)

    expected = _expected(scope)
    assert found == found_again
    assert found <= expected
    if endpoint != "/agent":
        assert found == expected
    elif expected:
        assert found  # the agent keeps only 5 chunks per step, so it may find fewer
    for name in set(DOCUMENTS) - expected | {"othertenant"}:
        assert _marker(name) not in text
        assert _marker(name) not in text_again
    assert "nobody" not in found or scope is None


def test_an_answer_cached_for_a_group_stops_being_served_when_the_document_leaves_it(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = _client(app_with_database, tenant_with_key)
    _upload_all(client)
    hr_document = next(d for d in client.get("/documents").json() if d["filename"] == "hr")

    before, _ = _call(client, "/query", ["hr"])
    patched = client.patch(f"/documents/{hr_document['id']}", json={"access_groups": ["public"]})
    after, text_after = _call(client, "/query", ["hr"])

    assert before == {"hr", "hr-and-engineering"}
    assert patched.status_code == 200
    assert after == {"hr-and-engineering"}
    assert _marker("hr") not in text_after


def test_every_scope_in_turn_gets_its_own_answer_with_the_cache_on(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """One corpus, one cache, every scope asked twice in a row: an answer cached for one
    scope is never what another scope is served.
    """
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = _client(app_with_database, tenant_with_key)
    _upload_all(client)

    for scope in [*SCOPES, *reversed(SCOPES)]:
        found, text = _call(client, "/query", scope)

        assert found == _expected(scope), scope
        for name in set(DOCUMENTS) - _expected(scope):
            assert _marker(name) not in text, (scope, name)
