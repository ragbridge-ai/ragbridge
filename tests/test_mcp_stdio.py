"""Tests for the ragbridge-mcp stdio entry point."""

import asyncio
import sys
from typing import Any

import httpx
import pytest
from mcp import Client, StdioServerParameters

from ragbridge.mcp_server.stdio import DEFAULT_BASE_URL, build_stdio_server, main


def test_tool_calls_are_forwarded_to_the_base_url_with_the_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    async def run() -> Any:
        server = build_stdio_server(
            "https://rag.example.com", "rb_secret", transport=httpx.MockTransport(handler)
        )
        async with Client(server) as client:
            return await client.call_tool("search_documents", {"query": "refunds"})

    result = asyncio.run(run())

    assert not result.is_error
    [request] = seen
    assert str(request.url) == "https://rag.example.com/search"
    assert request.headers["Authorization"] == "Bearer rb_secret"


def test_a_rejected_key_reaches_the_client_as_a_readable_tool_error() -> None:
    async def run() -> Any:
        server = build_stdio_server(
            "http://ragbridge",
            "rb_revoked",
            transport=httpx.MockTransport(lambda request: httpx.Response(401)),
        )
        async with Client(server) as client:
            return await client.call_tool("list_documents", {})

    result = asyncio.run(run())

    assert result.is_error
    assert "API key" in result.content[0].text


def test_main_refuses_to_start_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RAGBRIDGE_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code != 0
    assert "RAGBRIDGE_API_KEY" in str(exit_info.value.code)
    assert capsys.readouterr().out == ""


def test_the_default_base_url_is_a_local_ragbridge() -> None:
    assert DEFAULT_BASE_URL == "http://localhost:8000"


def test_the_real_process_speaks_mcp_over_stdio() -> None:
    """Start the entry point as a subprocess, the way Claude Desktop does.

    Listing tools makes no HTTP call, so no ragbridge needs to be running -
    this proves the process starts, keeps stdout clean for the protocol, and
    registers the shared tools.
    """
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ragbridge.mcp_server.stdio"],
        env={"RAGBRIDGE_API_KEY": "rb_test", "RAGBRIDGE_BASE_URL": "http://localhost:1"},
    )

    async def run() -> Any:
        async with Client(parameters) as client:
            return await client.list_tools()

    tools = asyncio.run(run()).tools

    assert sorted(tool.name for tool in tools) == ["ask", "list_documents", "search_documents"]
