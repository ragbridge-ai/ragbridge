"""Tests for the MCP tool definitions, over the SDK's in-memory client.

ragbridge itself is replaced by an ``httpx.MockTransport``, so these test
the tools' names, parameters, and error handling - not retrieval, which
``test_search.py`` and friends already cover.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
from mcp import Client
from mcp.server.mcpserver import Context

from ragbridge.mcp_server.client import RagbridgeClient
from ragbridge.mcp_server.tools import build_mcp_server

DOCUMENT_ID = "8f6f1d7e-6b3c-4a52-9d5e-0c1f7f6a2b11"

Handler = Callable[[httpx.Request], httpx.Response]


def _call_tool(handler: Handler, name: str, arguments: dict[str, Any]) -> Any:
    """Call one tool on a server whose ragbridge is ``handler``."""

    def client_for(ctx: Context) -> RagbridgeClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
        return RagbridgeClient(http)

    async def run() -> Any:
        async with Client(build_mcp_server(client_for)) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_the_server_exposes_exactly_the_three_documented_tools() -> None:
    async def run() -> Any:
        server = build_mcp_server(lambda ctx: RagbridgeClient(httpx.AsyncClient()))
        async with Client(server) as client:
            return await client.list_tools()

    tools = asyncio.run(run()).tools

    assert sorted(tool.name for tool in tools) == ["ask", "list_documents", "search_documents"]
    assert all(tool.description for tool in tools)


def test_search_documents_forwards_the_query_and_returns_the_chunks() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        hit = {
            "document_id": DOCUMENT_ID,
            "filename": "policy.txt",
            "chunk_index": 0,
            "content": "Refunds take five days.",
            "score": 0.9,
        }
        return httpx.Response(200, json={"results": [hit]})

    result = _call_tool(handler, "search_documents", {"query": "refunds", "top_k": 3})

    assert not result.is_error
    assert seen == {"path": "/search", "body": {"query": "refunds", "top_k": 3}}
    [hit] = result.structured_content["results"]
    assert hit["content"] == "Refunds take five days."


def test_ask_returns_the_answer_and_its_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/query"
        assert json.loads(request.content) == {"question": "How long do refunds take?"}
        return httpx.Response(200, json={"answer": "Five days.", "sources": []})

    result = _call_tool(handler, "ask", {"question": "How long do refunds take?"})

    assert result.structured_content == {"answer": "Five days.", "sources": []}


def test_list_documents_returns_the_documents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/documents"
        document = {
            "id": DOCUMENT_ID,
            "filename": "policy.txt",
            "content_type": "text/plain",
            "status": "ready",
            "error": None,
            "created_at": "2026-09-18T12:00:00Z",
        }
        return httpx.Response(200, json=[document])

    result = _call_tool(handler, "list_documents", {})

    [document] = result.structured_content["result"]
    assert (document["filename"], document["status"]) == ("policy.txt", "ready")


def test_a_rejected_api_key_becomes_a_tool_error_the_client_can_read() -> None:
    result = _call_tool(
        lambda request: httpx.Response(401, json={"detail": "invalid API key"}),
        "search_documents",
        {"query": "anything"},
    )

    assert result.is_error
    assert "API key" in result.content[0].text


def test_search_documents_output_schema_has_no_retrieval_detail() -> None:
    """``explain`` belongs to the REST API. MCP clients get the plain shape, so
    adding it to POST /search must not add fields to this tool's schema.
    """

    async def run() -> Any:
        server = build_mcp_server(lambda ctx: RagbridgeClient(httpx.AsyncClient()))
        async with Client(server) as client:
            return await client.list_tools()

    [tool] = [t for t in asyncio.run(run()).tools if t.name == "search_documents"]

    assert tool.output_schema is not None
    hit_fields = set(tool.output_schema["$defs"]["SearchHit"]["properties"])
    assert hit_fields == {"document_id", "filename", "chunk_index", "content", "score"}
    assert set(tool.output_schema["properties"]) == {"results"}
