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
from typing import Annotated

import pydantic_core
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

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


def _result_of(response: BaseModel) -> CallToolResult:
    """The tool result for ``response``, built the way the SDK builds it for a returned model.

    ``search_documents`` returns a ``CallToolResult`` so that one call can carry more fields
    (``retrieval``) than the output schema declares, which stays that of ``SearchResponse``:
    a client that never asks for ``explain`` sees exactly the result it always did. Fields
    that were never set are left out, so a rank that is ``null`` (the arm did not find the
    chunk) is kept and a field the server did not send is not invented.
    """
    data = response.model_dump(mode="json", by_alias=True, exclude_unset=True)
    text = pydantic_core.to_json(data, indent=2).decode()
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def build_mcp_server(client_for: ClientFactory, *, name: str = "ragbridge") -> MCPServer:
    """Create an MCP server exposing ragbridge's tenant-scoped search tools."""
    server = MCPServer(name, instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Find the passages in the user's documents that best match a query. "
            "Returns whole text chunks with their source file and a relevance score, "
            "best first. Use this to gather evidence, then reason over it yourself; "
            "call it again with a different query if the first results miss. "
            "Set explain=true to also get, for every passage, `retrieval`: the rank the "
            "vector search and the keyword search gave it (null means that search did not "
            "find it) and its rank before reranking, plus `candidate_count`. Use it to see "
            "why a passage did or did not come back."
        )
    )
    async def search_documents(
        query: str, ctx: Context, top_k: int = 5, explain: bool = False
    ) -> Annotated[CallToolResult, SearchResponse]:
        async with _open(client_for, ctx) as client:
            if explain:
                return _result_of(await client.search_explained(query, top_k))
            return _result_of(await client.search(query, top_k))

    @server.tool(
        description=(
            "Ask a question and get a finished answer written from the user's documents, "
            "with the sources it used. Prefer search_documents if you want to read the "
            "passages and reason over them yourself. `sources` lists every chunk the model "
            "was given: first the chunks retrieval scored (context_only=false, at most 5, "
            "best first), then neighbouring chunks given as surrounding text "
            "(context_only=true, score 0.0, not scored by retrieval)."
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
