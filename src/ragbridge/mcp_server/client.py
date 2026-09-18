"""A small client for a running ragbridge's REST API.

Both MCP transports use it (decision 8, docs/plans/phase-4.md): the mounted
``/mcp`` endpoint talks to its own app in-process, and the ``ragbridge-mcp``
stdio entry point talks to a server over the network. Either way the request
goes through the normal REST layer, so API-key auth and tenant scoping are
enforced in exactly one place and cannot drift between transports.
"""

from types import TracebackType
from typing import Self

import httpx

from ragbridge.api.documents import DocumentOut
from ragbridge.api.query import QueryResponse
from ragbridge.api.search import SearchResponse


class RagbridgeError(Exception):
    """A request to ragbridge failed; the message is safe to show a user."""


class RagbridgeClient:
    """Calls ragbridge's REST endpoints with an already-configured ``httpx`` client.

    The caller decides the base URL and the ``Authorization`` header, so
    this class knows nothing about how the caller was authenticated.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def search(self, query: str, top_k: int) -> SearchResponse:
        response = await self._request("POST", "/search", json={"query": query, "top_k": top_k})
        return SearchResponse.model_validate(response.json())

    async def ask(self, question: str) -> QueryResponse:
        response = await self._request("POST", "/query", json={"question": question})
        return QueryResponse.model_validate(response.json())

    async def list_documents(self) -> list[DocumentOut]:
        response = await self._request("GET", "/documents")
        return [DocumentOut.model_validate(item) for item in response.json()]

    async def _request(
        self, method: str, path: str, *, json: dict[str, object] | None = None
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as error:
            raise RagbridgeError(f"could not reach ragbridge: {error}") from error
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise RagbridgeError("ragbridge rejected the API key (missing, unknown, or revoked)")
        if response.is_error:
            raise RagbridgeError(f"ragbridge returned HTTP {response.status_code}")
        return response
