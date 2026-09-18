"""The MCP tool definitions, shared by both transports.

Defined once, here, so a tool's name, description, and parameters cannot
differ between the mounted HTTP transport and the stdio entry point
(decision 8, docs/plans/phase-4.md). How a request is authenticated is the
caller's business: ``build_mcp_server`` is handed a function that returns a
client for the current request.

``agent_query`` is deliberately not a tool (decision 9): an MCP client is
already an agent and can call ``search_documents`` several times itself.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ragbridge.api.documents import DocumentOut
from ragbridge.api.query import QueryResponse
from ragbridge.api.search import SearchResponse
from ragbridge.mcp_server.client import RagbridgeClient, RagbridgeError

ClientFactory = Callable[[Context], RagbridgeClient]
"""Returns a client for the request that triggered a tool call."""

INSTRUCTIONS = (
    "Search and ask questions over a company's own documents. "
    "Use list_documents to see what is searchable, search_documents to fetch "
    "relevant passages to reason over yourself, and ask for a finished answer."
)


@asynccontextmanager
async def _open(client_for: ClientFactory, ctx: Context) -> AsyncIterator[RagbridgeClient]:
    """Open the request's client, turning a ragbridge failure into a ``ToolError``.

    The SDK shows the model only a generic "Error executing tool" for an
    unexpected exception. A ``ToolError`` is an anticipated failure, and its
    message reaches the model - which is how it learns the API key was
    rejected instead of retrying blindly.
    """
    try:
        async with client_for(ctx) as client:
            yield client
    except RagbridgeError as error:
        raise ToolError(str(error)) from error


def build_mcp_server(client_for: ClientFactory, *, name: str = "ragbridge") -> MCPServer:
    """Create an MCP server exposing ragbridge's tenant-scoped search tools."""
    server = MCPServer(name, instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Find the passages in the user's documents that best match a query. "
            "Returns whole text chunks with their source file and a relevance score, "
            "best first. Use this to gather evidence, then reason over it yourself; "
            "call it again with a different query if the first results miss."
        )
    )
    async def search_documents(query: str, ctx: Context, top_k: int = 5) -> SearchResponse:
        async with _open(client_for, ctx) as client:
            return await client.search(query, top_k)

    @server.tool(
        description=(
            "Ask a question and get a finished answer written from the user's documents, "
            "with the sources it used. Prefer search_documents if you want to read the "
            "passages and reason over them yourself."
        )
    )
    async def ask(question: str, ctx: Context) -> QueryResponse:
        async with _open(client_for, ctx) as client:
            return await client.ask(question)

    @server.tool(
        description=(
            "List the documents that can be searched, with their processing status. "
            "Only documents whose status is 'ready' appear in search results."
        )
    )
    async def list_documents(ctx: Context) -> list[DocumentOut]:
        async with _open(client_for, ctx) as client:
            return await client.list_documents()

    return server
