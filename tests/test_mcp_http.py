"""End to end tests for the MCP transport mounted at /mcp.

The SDK's own client talks to the real app over streamable HTTP, so these
exercise the protocol, the bearer-key auth, and tenant scoping together.
The database is the real test database; embedder and chatter are fakes.
"""

import asyncio
import re
from typing import Any

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ragbridge.auth import generate_api_key
from ragbridge.chat import get_chatter

SECRET = "The launch code is 8842."


def _upload(app: FastAPI, key: str, filename: str, text: str) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    response = client.post("/documents", files={"file": (filename, text.encode(), "text/plain")})
    assert response.status_code == 201


Call = tuple[str, str | None, dict[str, Any]]
"""(API key, tool name, arguments). A tool name of None lists the tools."""


def _mcp(app: FastAPI, *calls: Call) -> list[Any]:
    """Make each call over MCP, in order, and return the results.

    All calls share one run of the app's lifespan: ASGITransport does not
    run it, and it is what starts the MCP session manager - which the SDK
    allows to start only once per app.
    """

    async def call_one(key: str, tool: str | None, arguments: dict[str, Any]) -> Any:
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {key}"}
        )
        async with http, Client(streamable_http_client("http://test/mcp", http_client=http)) as c:
            if tool is None:
                return await c.list_tools()
            return await c.call_tool(tool, arguments)

    async def run() -> list[Any]:
        async with app.router.lifespan_context(app):
            return [await call_one(*call) for call in calls]

    return asyncio.run(run())


def test_the_mounted_server_lists_the_three_tools(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    [result] = _mcp(app_with_database, (tenant_with_key, None, {}))

    assert sorted(tool.name for tool in result.tools) == [
        "ask",
        "list_documents",
        "search_documents",
    ]


def test_search_documents_returns_the_callers_own_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, "codes.txt", SECRET)

    [result] = _mcp(app_with_database, (tenant_with_key, "search_documents", {"query": SECRET}))

    assert not result.is_error
    [hit] = result.structured_content["results"]
    assert (hit["filename"], hit["content"]) == ("codes.txt", SECRET)


def test_search_documents_with_explain_says_which_retrieval_arm_found_the_chunk(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, "codes.txt", SECRET)

    plain, explained = _mcp(
        app_with_database,
        (tenant_with_key, "search_documents", {"query": SECRET}),
        (tenant_with_key, "search_documents", {"query": SECRET, "explain": True}),
    )

    [hit] = explained.structured_content["results"]
    assert hit["retrieval"]["vector_rank"] == 1
    assert hit["retrieval"]["keyword_rank"] == 1
    assert hit["retrieval"]["rank_before_rerank"] == 1
    assert explained.structured_content["candidate_count"] == 1
    assert "retrieval" not in plain.structured_content["results"][0]
    assert "candidate_count" not in plain.structured_content


def test_ask_returns_an_answer_with_sources(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, "codes.txt", SECRET)

    [result] = _mcp(app_with_database, (tenant_with_key, "ask", {"question": SECRET}))

    assert result.structured_content["answer"] == "Fake answer using 1 chunk(s)."
    assert len(result.structured_content["sources"]) == 1


def test_list_documents_lists_the_callers_documents(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, "codes.txt", SECRET)

    [result] = _mcp(app_with_database, (tenant_with_key, "list_documents", {}))

    assert [d["filename"] for d in result.structured_content["result"]] == ["codes.txt"]


def test_another_tenants_documents_are_invisible_through_every_tool(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, "codes.txt", SECRET)

    key = second_tenant_with_key
    searched, asked, listed = _mcp(
        app_with_database,
        (key, "search_documents", {"query": SECRET}),
        (key, "ask", {"question": SECRET}),
        (key, "list_documents", {}),
    )

    assert searched.structured_content["results"] == []
    assert asked.structured_content["sources"] == []
    assert listed.structured_content["result"] == []


def test_an_unknown_key_gets_a_readable_tool_error(app_with_database: FastAPI) -> None:
    [result] = _mcp(app_with_database, (generate_api_key(), "search_documents", {"query": "x"}))

    assert result.is_error
    assert "API key" in result.content[0].text


def test_a_request_with_no_bearer_token_is_rejected_with_401(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).post("/mcp", json={})

    assert response.status_code == 401


SIX_SECTIONS = "\n\n".join(
    f"Section {name}: a block of a document, written long enough that it stays a chunk of its"
    f" own instead of being merged with a neighbour, number {index}."
    for index, name in enumerate(["One", "Two", "Three", "Four", "Five", "Six"])
)


class _RecordingChatter:
    def __init__(self) -> None:
        self.context: list[str] = []

    async def answer(self, question: str, context: list[str]) -> str:
        self.context = context
        return "recorded"


def test_ask_sources_are_exactly_the_chunks_the_model_received(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Over MCP, with the tool's default of five retrieved chunks out of six."""
    recorder = _RecordingChatter()
    app_with_database.dependency_overrides[get_chatter] = lambda: recorder
    _upload(app_with_database, tenant_with_key, "doc.txt", SIX_SECTIONS)

    [result] = _mcp(app_with_database, (tenant_with_key, "ask", {"question": "Which section?"}))

    sources = result.structured_content["sources"]
    labels = re.findall(r"chunks? (\d+)(?:-(\d+))?\]", "\n".join(recorder.context))
    received = sorted(
        index for first, last in labels for index in range(int(first), int(last or first) + 1)
    )
    assert received, "the recorder should have seen labelled chunks"
    assert sorted(source["chunk_index"] for source in sources) == received
    assert sum(not source["context_only"] for source in sources) == 5
    assert [source["context_only"] for source in sources][-1] is True
    assert all(source["score"] == 0.0 for source in sources if source["context_only"])
