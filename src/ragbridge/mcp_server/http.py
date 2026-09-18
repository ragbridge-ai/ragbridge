"""The MCP streamable-HTTP transport, mounted into the FastAPI app at ``/mcp``.

One deployment, one database, one settings object, and the same
``Authorization: Bearer`` API key as every other endpoint (decision 8,
docs/plans/phase-4.md). A tool call is turned back into an ordinary REST
request against this same app, carrying the caller's own header, so the
existing ``get_tenant`` dependency authenticates it and every query stays
tenant-scoped - the MCP layer never sees a database session or a tenant.
"""

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ragbridge.mcp_server.client import RagbridgeClient
from ragbridge.mcp_server.tools import build_mcp_server

MCP_PATH = "/mcp"


class RequireBearer:
    """Reject a request with no bearer token before it reaches the MCP server.

    It only checks that a token is *present*. Whether the key is valid is
    decided by the REST layer on every tool call, so there is one place that
    knows how keys work. This just gives a client with no key at all a
    proper HTTP 401 at connect time instead of a failure on its first call.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if not headers.get(b"authorization", b"").lower().startswith(b"bearer "):
                response = JSONResponse({"detail": "invalid API key"}, status_code=401)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def mount_mcp(app: FastAPI) -> MCPServer:
    """Serve the MCP tools from ``app`` at ``/mcp``.

    The caller must run ``server.session_manager.run()`` for the life of the
    app (see ``main.lifespan``): mounted apps do not get their own lifespan.
    """

    def client_for(ctx: Context) -> RagbridgeClient:
        authorization = (ctx.headers or {}).get("authorization", "")
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://ragbridge",
            headers={"Authorization": authorization},
        )
        return RagbridgeClient(http)

    server = build_mcp_server(client_for)
    mcp_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Stateless: every request stands alone, which is what lets the
        # caller's own Authorization header be read on each tool call.
        stateless_http=True,
        json_response=True,
        # The SDK's DNS-rebinding guard only allows localhost Host headers,
        # which would reject every real deployment. It protects servers that
        # trust the network; this one requires a bearer key on every call.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # A Route, not a Mount: a Mount would redirect POST /mcp to /mcp/.
    app.router.routes.append(Route(MCP_PATH, endpoint=RequireBearer(mcp_app)))
    return server
