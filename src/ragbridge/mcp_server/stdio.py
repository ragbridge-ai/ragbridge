"""``ragbridge-mcp``: the MCP server for desktop clients such as Claude Desktop.

A thin proxy to a *running* ragbridge, not a second in-process instance
(decision 8, docs/plans/phase-4.md): a desktop client should not need a
``DATABASE_URL`` and a Redis connection just to ask a question. It serves
the same tools as the ``/mcp`` endpoint - both come from
``build_mcp_server`` - and forwards each call to ragbridge's REST API with
one fixed API key.

Configured by two environment variables, since it is a client of a server
rather than part of one: ``RAGBRIDGE_API_KEY`` (required) and
``RAGBRIDGE_BASE_URL`` (default ``http://localhost:8000``).

On stdio, stdout *is* the protocol channel, so nothing here may print to it.
"""

import os
import sys

import httpx
from mcp.server.mcpserver import Context, MCPServer

from ragbridge.mcp_server.client import RagbridgeClient
from ragbridge.mcp_server.tools import build_mcp_server

DEFAULT_BASE_URL = "http://localhost:8000"
REQUEST_TIMEOUT = 120.0
"""Seconds to wait for ragbridge. POST /query runs a language model, and a
local one can take far longer than httpx's 5-second default."""


def build_stdio_server(
    base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> MCPServer:
    """Create the MCP server, forwarding every tool call to ``base_url``.

    ``transport`` exists so tests can stand in for the network.
    """

    def client_for(ctx: Context) -> RagbridgeClient:
        http = httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        return RagbridgeClient(http)

    return build_mcp_server(client_for)


def main() -> None:
    api_key = os.environ.get("RAGBRIDGE_API_KEY", "")
    if not api_key:
        # A string passed to sys.exit is written to stderr, never to stdout.
        sys.exit("ragbridge-mcp: set RAGBRIDGE_API_KEY to a ragbridge API key.")
    base_url = os.environ.get("RAGBRIDGE_BASE_URL", DEFAULT_BASE_URL)
    build_stdio_server(base_url, api_key).run("stdio")


if __name__ == "__main__":
    main()
