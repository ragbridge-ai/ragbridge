"""Tests for POST /agent, with FakePlanner - no real model is called."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.agent.planner import FakePlanner, PlannerDecision, get_planner
from ragbridge.auth import generate_api_key
from ragbridge.config import Settings, get_settings


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _script(app: FastAPI, *queries: str) -> None:
    """Make the planner ask for one search per query, then answer."""
    planner = FakePlanner([PlannerDecision(action="search", query=q) for q in queries])
    app.dependency_overrides[get_planner] = lambda: planner


def _upload_two_documents(client: TestClient) -> None:
    client.post("/documents", files={"file": ("a.txt", b"Refunds take five days.", "text/plain")})
    client.post("/documents", files={"file": ("b.txt", b"Cancellations are free.", "text/plain")})


def test_agent_response_shape_for_a_multi_step_run(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _upload_two_documents(client)
    _script(app_with_database, "cancellation policy")

    response = client.post("/agent", json={"question": "refund policy"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"answer", "sources", "steps", "step_count"}
    assert body["steps"] == [
        {"query": "refund policy", "results": 2},
        {"query": "cancellation policy", "results": 2},
    ]
    assert body["step_count"] == 2
    # Both searches found the same two chunks; they reach the answer once each.
    assert body["answer"] == "Fake answer using 2 chunk(s)."
    assert len(body["sources"]) == 2
    assert set(body["sources"][0]) == {
        "document_id",
        "filename",
        "chunk_index",
        "snippet",
        "score",
        "context_only",
    }
    # the agent does not add neighbouring chunks, so none of its sources is context-only
    assert [source["context_only"] for source in body["sources"]] == [False, False]


def test_agent_with_a_planner_that_answers_immediately_takes_one_step(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _upload_two_documents(client)

    response = client.post("/agent", json={"question": "refund policy"})

    body = response.json()
    assert body["step_count"] == 1
    assert body["steps"][0]["query"] == "refund policy"


def test_agent_request_can_ask_for_fewer_steps_than_the_server_allows(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _script(app_with_database, "one", "two", "three")

    response = client.post("/agent", json={"question": "q", "max_steps": 2})

    assert response.json()["step_count"] == 2


def test_agent_request_cannot_raise_the_server_step_ceiling(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(agent_max_steps=2)
    client = _client(app_with_database, tenant_with_key)
    _script(app_with_database, "one", "two", "three", "four")

    response = client.post("/agent", json={"question": "q", "max_steps": 10})

    assert response.json()["step_count"] == 2


def test_agent_rejects_a_max_steps_below_one(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).post(
        "/agent", json={"question": "q", "max_steps": 0}
    )

    assert response.status_code == 422


def test_agent_with_no_authorization_header_is_rejected(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).post("/agent", json={"question": "q"})

    assert response.status_code == 401


def test_agent_with_an_unknown_key_is_rejected(app_with_database: FastAPI) -> None:
    client = _client(app_with_database, generate_api_key())

    response = client.post("/agent", json={"question": "q"})

    assert response.status_code == 401


def test_agent_never_returns_another_tenants_content_on_any_step(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    secret = "The launch code is 8842."
    _client(app_with_database, tenant_with_key).post(
        "/documents", files={"file": ("a.txt", secret.encode(), "text/plain")}
    )
    _script(app_with_database, secret)

    response = _client(app_with_database, second_tenant_with_key).post(
        "/agent", json={"question": secret}
    )

    body = response.json()
    assert body["sources"] == []
    assert [step["results"] for step in body["steps"]] == [0, 0]
    assert body["answer"] == "Fake answer using 0 chunk(s)."
