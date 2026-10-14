"""Tests for ``access_groups`` on POST /search, /query and /agent.

Documents are written straight to the database here, labelled by hand: setting groups
through the document endpoints is tested with those endpoints.
"""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.agent.planner import FakePlanner, PlannerDecision, get_planner
from ragbridge.chat import get_chatter
from ragbridge.config import Settings, get_settings
from tests.helpers import add_document, tenant_id_for_key

DOCUMENTS = {
    "public": ("Refunds take five days.", ["public"]),
    "hr": ("Salaries are reviewed in March.", ["hr"]),
    "both": ("Leave requests go to the manager.", ["hr", "public"]),
    "nobody": ("A draft that nobody may read.", []),
}
QUESTION = "refunds salaries leave draft"


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _seed(app: FastAPI, key: str) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory

    async def run() -> None:
        tenant_id = await tenant_id_for_key(session_factory, key)
        async with session_factory() as session:
            for name, (text, groups) in DOCUMENTS.items():
                await add_document(session, tenant_id, name, [text], groups)
            await session.commit()

    asyncio.run(run())


def _ask(client: TestClient, endpoint: str, groups: list[str] | None) -> set[str]:
    """The filenames an endpoint returns for ``groups``; ``None`` leaves the field out."""
    body: dict[str, Any] = {"query" if endpoint == "/search" else "question": QUESTION}
    if groups is not None:
        body["access_groups"] = groups
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    items = response.json()["results" if endpoint == "/search" else "sources"]
    return {item["filename"] for item in items}


@pytest.mark.parametrize("endpoint", ["/search", "/query", "/agent"])
@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        (["public"], {"public", "both"}),
        (["hr"], {"hr", "both"}),
        (["public", "hr"], {"public", "hr", "both"}),
        (["unknown"], set()),
        ([], set()),
        (None, {"public", "hr", "both", "nobody"}),
    ],
)
def test_each_endpoint_returns_only_documents_sharing_a_group(
    app_with_database: FastAPI,
    tenant_with_key: str,
    endpoint: str,
    groups: list[str] | None,
    expected: set[str],
) -> None:
    _seed(app_with_database, tenant_with_key)

    found = _ask(_client(app_with_database, tenant_with_key), endpoint, groups)

    assert found == expected


@pytest.mark.parametrize("endpoint", ["/search", "/query", "/agent"])
@pytest.mark.parametrize(
    "groups",
    [["bad name!"], ["-leading-dash"], ["é"], [""], ["x" * 65], [f"g{n}" for n in range(33)], "hr"],
)
def test_an_invalid_group_list_is_rejected(
    app_with_database: FastAPI, tenant_with_key: str, endpoint: str, groups: object
) -> None:
    body = {"query" if endpoint == "/search" else "question": "anything", "access_groups": groups}

    response = _client(app_with_database, tenant_with_key).post(endpoint, json=body)

    assert response.status_code == 422


def test_an_unlabelled_document_is_not_found_by_the_agent_in_any_step(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _seed(app_with_database, tenant_with_key)
    planner = FakePlanner(
        [
            PlannerDecision(action="search", query="salaries"),
            PlannerDecision(action="search", query="draft"),
        ]
    )
    app_with_database.dependency_overrides[get_planner] = lambda: planner

    response = _client(app_with_database, tenant_with_key).post(
        "/agent", json={"question": "refunds", "access_groups": ["public"]}
    )

    assert response.status_code == 200
    assert {source["filename"] for source in response.json()["sources"]} == {"public", "both"}


class _RecomputedChatter:
    async def answer(self, question: str, context: list[str]) -> str:
        return "recomputed"


def _cached_app(app: FastAPI) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(answer_cache_enabled=True)


def test_an_answer_cached_for_one_group_is_not_served_to_another(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _cached_app(app_with_database)
    _seed(app_with_database, tenant_with_key)
    client = _client(app_with_database, tenant_with_key)

    assert _ask(client, "/query", ["hr"]) == {"hr", "both"}
    assert _ask(client, "/query", ["public"]) == {"public", "both"}
    assert _ask(client, "/query", ["hr"]) == {"hr", "both"}


def test_no_scope_and_an_empty_scope_do_not_share_cached_answers(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _cached_app(app_with_database)
    _seed(app_with_database, tenant_with_key)
    client = _client(app_with_database, tenant_with_key)

    assert _ask(client, "/query", []) == set()
    assert _ask(client, "/query", None) == {"public", "hr", "both", "nobody"}
    assert _ask(client, "/query", []) == set()
    assert _ask(client, "/query", ["hr"]) == {"hr", "both"}


def test_the_same_groups_in_another_order_share_a_cached_answer(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _cached_app(app_with_database)
    _seed(app_with_database, tenant_with_key)
    client = _client(app_with_database, tenant_with_key)
    first = client.post("/query", json={"question": QUESTION, "access_groups": ["hr", "public"]})
    app_with_database.dependency_overrides[get_chatter] = lambda: _RecomputedChatter()

    second = client.post("/query", json={"question": QUESTION, "access_groups": ["public", "hr"]})
    duplicated = client.post(
        "/query", json={"question": QUESTION, "access_groups": ["public", "hr", "hr"]}
    )

    assert second.json() == first.json()
    assert duplicated.json() == first.json()
    assert second.json()["answer"] != "recomputed"


def test_the_context_around_an_answer_is_limited_to_the_groups_too(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """A document's chunks normally share its groups, so the chunk next to a visible one is
    visible too and a missing filter on the neighbours cannot be seen. Here the middle chunk
    carries another group, by hand, so that it can.
    """
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def seed() -> None:
        tenant_id = await tenant_id_for_key(session_factory, tenant_with_key)
        async with session_factory() as session:
            await add_document(
                session,
                tenant_id,
                "mixed",
                ["alpha public one", "secret hr middle", "alpha public three"],
                ["public"],
                chunk_groups=[["public"], ["hr"], ["public"]],
            )
            await session.commit()

    asyncio.run(seed())
    client = _client(app_with_database, tenant_with_key)

    scoped = client.post("/query", json={"question": "alpha", "access_groups": ["public"]})
    unscoped = client.post("/query", json={"question": "alpha"})

    assert {source["snippet"] for source in scoped.json()["sources"]} == {
        "alpha public one",
        "alpha public three",
    }
    assert "secret hr middle" in {source["snippet"] for source in unscoped.json()["sources"]}
