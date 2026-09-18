# Phase 4 — Agents and MCP

This document plans Phase 4 from the [roadmap in AGENTS.md](../../AGENTS.md#4-roadmap).
Phase 1 built a RAG pipeline, Phase 2 made its retrieval better and measurable, and
Phase 3 made it safe to expose to more than one caller. Phase 4 lets it take more
than one step, and lets other tools call it.

Today `POST /query` is single-shot: embed the question, retrieve once, rerank,
answer. That is the right shape for "what does the refund policy say?" and the wrong
shape for "how does our refund policy differ from our cancellation policy?" - a
question whose answer lives in two places that one embedding cannot point at
simultaneously. Phase 4 adds a bounded loop that can search more than once, and an
MCP server so a client like Claude Desktop can search a tenant's documents directly.

## Decisions

1. **The agent loop is hand-rolled, not LangGraph** - a deliberate deviation from
   the roadmap, which named LangGraph, and AGENTS.md is updated to match rather
   than left contradicting this plan.
   `langgraph` hard-requires `langchain-core<2,>=1.4.7`, which would put the
   LangChain ecosystem into this project's **runtime** dependencies. Phase 1
   decision 3 already rejected LangChain once, and this project wrote its own
   chunker instead. Meanwhile everything the loop needs - retrieval, fusion,
   reranking, generation - already exists as plain async functions, so what
   LangGraph actually contributes here is a `StateGraph`, one conditional edge,
   and a step counter. That was prototyped against the real library
   (`langgraph==1.2.11`) before this decision: it works, and it is about twenty
   lines of graph wiring around the same function calls a `while` loop would make.
   **What this gives up, stated honestly:** checkpointing (resuming a partial run),
   built-in streaming primitives, and a vocabulary other developers already know.
   If any of those becomes a real requirement rather than a hypothetical one, the
   loop lives behind a single function and can be swapped. Related: `langchain-core`
   is already installed in this project **as a dev-only dependency** of `ragas`
   (Phase 2). Keeping it dev-only is precisely the point of this decision.
2. **Deciding what to do next is its own `Protocol`, not a new method on `Chatter`.**
   `Planner` joins `Embedder`, `Chatter`, `Reranker`, `JobQueue`, and `Cache` - the
   sixth protocol following the same shape, for the same reason: a real
   implementation that calls a provider, and a fake one that lets CI exercise the
   whole path without a network call. `Chatter.answer(question, context) -> str`
   stays focused on the one thing it does. `FakePlanner` can be handed a scripted
   sequence of decisions, which is the only reason a *multi-step* agent is testable
   deterministically at all.
3. **A planner decision that cannot be parsed means "answer now" - never a crash,
   never a retry loop.** This is not defensive boilerplate; it is grounded in this
   project's own measurements. `docs/adr/0004-...` recorded that `llama3.2` - the
   default `CHAT_MODEL` - failed RAGAS's structured-output contract on **24 of 25
   records**, echoing the JSON schema back instead of filling it in. An agent whose
   control flow depends on structured output from that model must therefore treat
   unparseable output as a normal, expected event. The consequence is that a weak
   model degrades to exactly today's single-shot RAG behaviour rather than failing,
   and a strong model gets multi-step retrieval. Nothing in between breaks.
4. **The step ceiling is enforced in code, not in the prompt.** `AGENT_MAX_STEPS`
   (default `3`) bounds the loop itself. A request may ask for fewer, never more -
   the same shape as `top_k`, which Pydantic caps at 20 regardless of what a caller
   sends. An agent that decides its own budget is a cost incident waiting for a
   prompt injection; "you may search at most three times" in a system prompt is a
   suggestion, while a `while step < max_steps` is a guarantee.
5. **Chunks retrieved across steps are de-duplicated by chunk id, keeping the best
   score.** Successive searches on a related question will return overlapping
   chunks - that is a sign retrieval is working, not a bug. Without de-duplication
   the final context wastes its budget on repeats and over-weights whatever matched
   twice, which is the opposite of what a second search was for.
6. **`POST /agent` is its own endpoint, and it is synchronous.**
   Not `POST /query` with `mode: "agent"`: that field already selects the
   *retrieval* arm (`hybrid` / `vector` / `keyword`), and overloading one field
   with a second, unrelated axis makes both harder to reason about. Synchronous
   rather than a Phase 3 background job because polling is a poor shape for an
   interactive question, and decision 4's step ceiling already bounds how long a
   run can take. The response carries **the steps taken** - each search query and
   how many results it found - because a multi-step answer that arrives with no
   record of how it was reached is not debuggable when it is wrong.
7. **No conversation memory in this phase.** Each `/agent` call is independent.
   Sessions, history, and follow-up questions are a coherent feature in their own
   right, with their own storage and their own tenancy questions; bolting a
   `session_id` onto this phase would be the beginning of that feature rather than
   the end of it. Deliberately out of scope, not overlooked.
8. **One set of MCP tool definitions, two transports.** The HTTP transport is
   mounted into the existing FastAPI app at `/mcp`, so it shares one deployment,
   one database, one settings object, and the same `Authorization: Bearer` key as
   every other endpoint. A separate `ragbridge-mcp` stdio entry point serves desktop
   clients like Claude Desktop, and is a **thin HTTP proxy to a running ragbridge**
   rather than a second in-process instance - a desktop client should not need a
   `DATABASE_URL` and a Redis connection to ask a question. The two transports
   register the same tools from one module, because tool descriptions that drift
   between transports are a bug users would experience as "it works in one client
   and not the other".
9. **MCP exposes `search_documents`, `ask`, and `list_documents` - not the agent.**
   `search_documents` is the most MCP-native of the three: it hands back chunks and
   lets the *client's* model do the reasoning with its own fresh context, which is
   the entire point of the protocol. `ask` returns a finished server-side answer for
   clients that want one. `list_documents` lets a client see what is actually
   searchable before it starts guessing. `agent_query` is deliberately **excluded**:
   an MCP client is already an agent, and it can call `search_documents` several
   times itself - exposing ours would nest a slow loop inside a loop that is better
   informed than it is.
10. **The `mcp` SDK's API was verified, not recalled.** `mcp` 2.x renamed `FastMCP`
    to `MCPServer` (`from mcp.server.mcpserver import MCPServer`) and renamed
    `Tool.inputSchema` to `Tool.input_schema`. Nearly every MCP example in
    circulation still says `FastMCP`, and code written from memory fails at import
    with a message that explicitly suggests pinning `mcp<2`. This is the fourth
    version-specific incompatibility this project has hit after `ragas`,
    `langfuse`, and `litellm`; it gets an ADR for the same reason those did.

## Data model changes

None. Decision 7 keeps the agent stateless per request, and MCP reads through the
same tenant-scoped queries `POST /query` and `GET /documents` already use.

## API changes

- **New `POST /agent`**: `{question, max_steps?}` →
  `{answer, sources[], steps[], step_count}`. Each entry in `steps` is
  `{query, results}` - what it searched for and how many chunks came back.
- **New MCP transport mounted at `/mcp`** (streamable HTTP, not a REST endpoint),
  authenticated with the same `Authorization: Bearer <key>` as everything else.
- No changes to `POST /query`, `POST /documents`, or any existing endpoint.

## New settings

| Setting | Default | Meaning |
|---|---|---|
| `AGENT_MAX_STEPS` | `3` | Hard ceiling on retrieval rounds per `/agent` call |
| `AGENT_PLANNER_MODEL` | `""` | Chat model for planning decisions; empty means reuse `CHAT_MODEL` |

`AGENT_PLANNER_MODEL` exists because planning and answering have genuinely
different requirements: planning needs reliable structured output (decision 3),
answering needs good prose. An installation running a small local model for answers
may want a stronger one deciding what to search for, without paying that model's
price for every answer. Empty by default, so a single-model setup stays a
single-model setup.

The `ragbridge-mcp` stdio entry point reads `RAGBRIDGE_BASE_URL` (default
`http://localhost:8000`) and `RAGBRIDGE_API_KEY` from its environment - it is a
client of a running server (decision 8), so these are its connection details rather
than application settings.

## Steps

1. `Planner` protocol, structured decisions, and graceful fallback - no endpoint yet
2. The agent loop and `POST /agent`
3. MCP tools and the mounted HTTP transport at `/mcp`
4. The `ragbridge-mcp` stdio entry point, for desktop clients
5. ADRs, README, and the `v0.3.0` wrap-up

Each step is built as several small, focused commits on its own feature branch.

## Step 1 detail — the Planner protocol

Branch: `feat/phase-4-planner` (created from `main`, off the merged Phase 3).
This step builds the thing that decides *whether to search again and for what*, and
nothing that calls it - the loop arrives in step 2. Splitting it this way means the
hard part (getting a structured decision out of an unreliable model, safely) lands
with its own tests, before any endpoint depends on it.

Proposed commit breakdown:

1. **`docs: add Phase 4 plan`** (this document), and update AGENTS.md's roadmap and
   tech-stack table so neither still claims LangGraph (decision 1).
2. **`feat(config): add agent settings`**
   - `AGENT_MAX_STEPS: int = 3` and `AGENT_PLANNER_MODEL: str = ""`, documented in
     `.env.example`. Inert until read, so this lands safely on its own.
3. **`feat(agent): add the Planner protocol with LiteLLM and fake implementations`**
   - New `src/ragbridge/agent/planner.py`. A frozen `PlannerDecision` dataclass with
     two shapes - search with a query, or answer now - expressed as
     `action: Literal["search", "answer"]` plus an optional `query`.
   - `Planner` Protocol: `async def plan(self, question: str, findings:
     list[str], step: int, max_steps: int) -> PlannerDecision`. `findings` is the
     snippets gathered so far, so the planner can judge whether the question is
     already covered.
   - `LiteLLMPlanner`: calls `litellm.acompletion` with `AGENT_PLANNER_MODEL` or
     `CHAT_MODEL`, asking for a small JSON object. Parses it defensively -
     `json.loads` failure, a missing key, an unknown `action`, or a `search` with
     an empty query **all** resolve to "answer now" (decision 3), each logged so a
     misbehaving model is visible rather than silent.
   - `FakePlanner`: constructed with a scripted list of decisions and returns them
     in order, falling back to "answer" once exhausted. This is what makes a
     multi-step loop testable without a real model - the same role `FakeChatter`
     plays for generation, extended to control flow.
   - `get_planner` FastAPI dependency, overridable in tests exactly like
     `get_embedder`.
   - Tests: a well-formed JSON search decision parses; a well-formed answer
     decision parses; malformed JSON, a missing `action`, an unknown `action`, and
     a `search` with an empty query each fall back to "answer" rather than raising;
     `FakePlanner` returns its script in order and then keeps answering. The
     fallback cases are the point of this step and get the most tests, because
     decision 3 says they are the *expected* path on this project's default model,
     not an edge case.
