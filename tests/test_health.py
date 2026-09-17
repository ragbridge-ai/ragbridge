from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_returns_ok_when_database_is_reachable(
    app_with_database: FastAPI,
) -> None:
    client = TestClient(app_with_database)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_returns_503_when_database_is_unreachable(
    app_with_unreachable_database: FastAPI,
) -> None:
    client = TestClient(app_with_unreachable_database)

    response = client.get("/health/ready")

    assert response.status_code == 503
