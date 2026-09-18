# 7. An MCP server on the `mcp` 2.x SDK, reaching data through the REST API

Date: 2026-09-18

## Status

Accepted.

## Context

Phase 4 lets a client such as Claude Desktop search a tenant's documents directly,
through the Model Context Protocol (MCP). Two things needed a decision: which SDK
API to build on, and how an MCP tool call should reach the data.

Everything below was verified against the installed version (`mcp==2.2.0`) and
by running it, before being written into `ragbridge.mcp_server` - the same lesson
`docs/adr/0004-...` and `docs/adr/0005-...` recorded for RAGAS, Langfuse and LiteLLM.
This is the fourth version-specific incompatibility this project has hit.

### The 2.x API differs from most published examples

- `FastMCP` is now `MCPServer`, imported from `mcp.server.mcpserver`. Code written
  from older examples fails at import.
- `Tool.inputSchema` is now `Tool.input_schema`.
- The SDK's own HTTP client is built on **`httpx2`**, not `httpx`. Only tests use
  it here; the application's own client uses plain `httpx`.
- A tool that raises an ordinary exception shows the model only a generic
  `Error executing tool <name>`. Only a **`ToolError`** carries its message to the
  model. A rejected API key therefore has to become a `ToolError`, or the model
  cannot tell an authentication failure from a crash and retries blindly.
- A mounted ASGI app gets no lifespan of its own. The streamable-HTTP session
  manager must be started from the host app's lifespan, and the SDK allows
  `session_manager.run()` **once per instance** - so the server is built per app in
  `create_app()`, and a test that runs the lifespan must run it once per test.
- The SDK's DNS-rebinding guard defaults to allowing only localhost `Host` headers,
  which would reject every real deployment.
- The SDK's built-in auth is OAuth-shaped (issuer URLs, scopes, token verification).

### How should a tool call reach the data?

An MCP tool could open its own database session and resolve its own tenant. That
duplicates the wiring `POST /query` already has (embedder, reranker, cache, tracing,
tenant filtering) and, in tests, escapes `app.dependency_overrides`, so fakes would
not apply.

## Decision

1. **Use `MCPServer` from `mcp>=2,<3`**, with the tools defined once in
   `ragbridge.mcp_server.tools.build_mcp_server`, so a tool's name, description and
   parameters cannot drift between transports.
2. **Every tool call is an ordinary REST request** made by a shared
   `RagbridgeClient`, carrying the caller's own `Authorization: Bearer` key.
   - The mounted `/mcp` transport sends it **in-process** over ASGI to the same
     FastAPI app.
   - The `ragbridge-mcp` stdio entry point sends it **over the network** to a running
     server with one fixed key. It is a thin proxy, so a desktop client needs no
     `DATABASE_URL` or Redis connection.
   The REST layer therefore authenticates and tenant-scopes every call in exactly one
   place, and the MCP layer never holds a database session or a tenant.
3. **Three tools: `search_documents`, `ask`, `list_documents`. Not the agent.**
   `search_documents` hands back whole chunks for the *client's* model to reason
   over, which is the point of the protocol. An MCP client is already an agent and can
   call `search_documents` several times itself; exposing `/agent` would nest a slow
   loop inside a better-informed one. Whole chunks needed a new `POST /search`, since
   `POST /query` returns only 300-character snippets.
4. **The `/mcp` transport is stateless** (`stateless_http=True`, `json_response=True`),
   so every request stands alone and its own `Authorization` header is read on each
   call. It is registered as a Starlette `Route`, not a `Mount`: a `Mount` at `/mcp`
   answers `POST /mcp` with a `307` redirect to `/mcp/`, which some clients do not
   follow. A small ASGI wrapper returns `401` at connect time when no bearer token is
   present.
5. **The SDK's OAuth machinery is not used.** A single API key does not need an
   issuer and scopes.
6. **The DNS-rebinding guard is disabled for `/mcp`.** It protects servers that trust
   the network; every call here requires a bearer key.

## Consequences

- **One source of truth for auth and tenancy.** Tenant isolation through MCP is
  tested end to end (all three tools, two tenants) against the real app, using the
  SDK's own client.
- **Validity of a key is checked on each tool call, not at connect time.** The
  wrapper only checks that a token is *present*. A client with a wrong key connects
  and then receives a readable tool error on its first call. Checking validity at
  connect time would mean a second copy of the key logic.
- **Each MCP tool call costs one extra in-process HTTP hop.** This was not measured;
  it is small next to the embedding and generation calls behind it.
- **Stateless means no server-initiated messages or resumable sessions.** None of the
  three tools needs them.
- **`mcp` is now a runtime dependency**, pinned to `>=2,<3` because a major version
  already renamed the core class once. `httpx` moved from a dev to a runtime dependency.
- **Verified by hand** against a real `uvicorn` server with Postgres, Redis and
  Ollama, through a real `ragbridge-mcp` subprocess: `list_documents`,
  `search_documents` and `ask` returned correct results, and a wrong key produced a
  readable error. CI itself never calls a real model.
