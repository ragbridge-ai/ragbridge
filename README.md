# ragbridge

Open-source, ready-to-use RAG (Retrieval-Augmented Generation) service. It lets any
existing application (for example a Laravel, Symfony, or WordPress app) add chat and
smart search over its own data, without moving the application to Python.

## Status

**v1.0.0 — Deploy and release.** A RAG service for existing applications: document
upload (text, Markdown, PDF), hybrid retrieval, answers with sources, API keys and
multi-tenancy, background processing, caching, Langfuse tracing, a multi-step agent
endpoint, and an MCP server. `docker-compose.prod.yml` runs it on a single Linux
server behind a TLS proxy; the [deployment guide](docs/deployment.md) says exactly what
was verified and what was not - **notably, it has not yet been run on a real server or
with a real domain.** See [AGENTS.md](AGENTS.md) for the roadmap and
[docs/plans/phase-5.md](docs/plans/phase-5.md) for this phase's decisions.

Documentation: [demo](docs/demo.md) (a real run, output included) ·
[deployment](docs/deployment.md) · [evaluation](docs/evaluation.md) ·
[decisions (ADRs)](docs/adr/).

## Requirements

- Docker (runs PostgreSQL + pgvector, Redis, and the app and worker themselves)
- [Ollama](https://ollama.com) running on your machine, for embeddings and chat by
  default (or a hosted provider instead - see Configuration)
- Python 3.12 and [uv](https://docs.astral.sh/uv/), only if you want to run the app
  outside Docker

## Getting started

```bash
cp .env.example .env
ollama pull nomic-embed-text
ollama pull llama3.2
docker compose up --build
```

This starts PostgreSQL, Redis, the API, and the background worker. The API is then
available at `http://localhost:8000`, with a liveness check at
`http://localhost:8000/health` and a readiness check (it queries the database, and
returns `503` if it is down) at `http://localhost:8000/health/ready` - the only two
endpoints that need no API key. Database migrations run automatically every time the
app container starts.

Every other endpoint needs a key. Create a tenant and its first key:

```bash
uv run ragbridge-admin create-tenant --name acme
```

This prints an API key once - it is stored only as a hash, so save it now. Use it as
`Authorization: Bearer <key>` on every request below.

### Running without Docker

```bash
uv sync
docker compose up -d postgres redis
uv run alembic upgrade head
uv run uvicorn ragbridge.main:app --reload
# in another terminal, for large uploads to actually get processed:
uv run arq ragbridge.worker.WorkerSettings
```

## Usage

### Upload a document

```bash
curl -H "Authorization: Bearer <key>" \
  -F "file=@notes.md;type=text/markdown" http://localhost:8000/documents
```

Text (`text/plain`), Markdown (`text/markdown`), and PDF (`application/pdf`) are
supported. Uploading the same content twice returns the existing document instead
of creating a duplicate (per tenant - two tenants uploading the same file each get
their own document). The response includes a `status`
(`pending` / `processing` / `ready` / `failed`): uploads at or under
`ASYNC_PROCESSING_THRESHOLD` come back `ready` immediately; larger ones come back
`pending` right away and are processed by the worker - poll
`GET /documents/{id}` until `status` is no longer `pending`/`processing`. A `failed`
document's `error` field explains why (a corrupt PDF, for example).

### Ask a question

```bash
curl -X POST http://localhost:8000/query \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "What does this document say?", "top_k": 5}'
```

Returns `{"answer": "...", "sources": [...]}`. Each source reports the originating
document, its chunk position, a text snippet, and a relevance score. Retrieval is
hybrid by default (vector + keyword search, merged with reciprocal rank fusion -
see [ADR 0003](docs/adr/0003-hybrid-search-with-reciprocal-rank-fusion.md)); pass
`"mode": "vector"` or `"mode": "keyword"` in the request to use a single method
instead of `RETRIEVAL_MODE`'s default. Only documents belonging to the calling
tenant's key are ever searched.

### See why a chunk was found

Add `"explain": true` to a `/query` or `/search` request to get, for every returned
chunk, how retrieval found it:

```bash
curl -X POST http://localhost:8000/search \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"query": "refund policy", "top_k": 2, "explain": true}'
```

Each result gains a `retrieval` object, and the response gains `candidate_count` (how
many chunks were found before reranking narrowed them to `top_k`). The scores below are
illustrative:

```json
{
  "results": [
    {
      "filename": "policy.md",
      "content": "...",
      "score": 0.0323,
      "retrieval": {
        "vector_rank": 1,
        "keyword_rank": 3,
        "fused_score": 0.0323,
        "rank_before_rerank": 1
      }
    }
  ],
  "candidate_count": 12
}
```

- `vector_rank` and `keyword_rank` are the chunk's 1-based position in each retrieval arm.
  **`null` means that arm did not find the chunk at all** - for example, a chunk with
  none of the question's words has no `keyword_rank`.
- `fused_score` is the score retrieval gave the chunk before reranking: the merged score
  in `hybrid` mode, that arm's own score in `vector` or `keyword` mode. With reranking
  off, it equals `score`.
- `rank_before_rerank` is the chunk's position before the reranker ran. When it always
  equals the chunk's position in the results, reranking is off (the default).

Without `explain` the response is exactly what it was before. `/agent` and the MCP tools
do not offer it: the agent merges chunks found by several searches, so a single arm's
rank has no clear meaning there.

### Search without generating an answer

```bash
curl -X POST http://localhost:8000/search \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"query": "refund policy", "top_k": 5}'
```

Returns `{"results": [...]}`: the same retrieval and reranking as `/query`, stopping
before generation. Each result is a **whole chunk** (`content`, not a snippet) with its
document, chunk position, and score - for callers that do their own reasoning over the
text.

### Ask a multi-step question

```bash
curl -X POST http://localhost:8000/agent \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "How does our refund policy differ from our cancellation policy?"}'
```

Returns `{"answer": "...", "sources": [...], "steps": [...], "step_count": N}`. The agent
searches for the question, then a planner model decides whether another search with a
different query would help, up to `AGENT_MAX_STEPS` searches. `steps` lists each query
and how many chunks it found, so a wrong answer can be traced to what was searched.
Chunks found by several searches appear once. A request may pass `"max_steps"` to use
fewer searches, never more than the server allows.

The planner must reply with a small JSON object. **If it cannot** - a reply that is not
valid JSON, names an unknown action, or asks to search for nothing - the agent answers
with what it has found so far, so `/agent` degrades to `/query` behaviour instead of
failing ([ADR 0006](docs/adr/0006-hand-rolled-agent-loop-instead-of-langgraph.md)).

**Measured, and worth knowing before you rely on it:** how well `/agent` works depends almost
entirely on the chat model. On a test built for it (answers reachable only through a chain
of documents), the default `llama3.2` was **not** meaningfully better than `/query` - it
usually decided one search was enough. `qwen2.5:7b` answered **29 of 30** two-step questions
correctly, twice. Three-step questions stay unreliable even then (13-33% correct). On a 16 GB
laptop `qwen2.5:7b` also scored 43/43 on a 43-question answering test in every run, at about
half `llama3.2`'s speed. Set it with `CHAT_MODEL=ollama/qwen2.5:7b`. Full method, numbers and
caveats: [docs/evaluation.md](docs/evaluation.md).
Each call is independent; there are no sessions or follow-up questions.

### List, fetch, and delete documents

```bash
curl -H "Authorization: Bearer <key>" http://localhost:8000/documents
curl -H "Authorization: Bearer <key>" http://localhost:8000/documents/<id>
curl -H "Authorization: Bearer <key>" -X DELETE http://localhost:8000/documents/<id>
```

## Multi-tenancy and API keys

Every request except `GET /health` and `GET /health/ready` needs
`Authorization: Bearer <key>`; a key
belongs to exactly one tenant, and a tenant's documents, chunks, and answers are
invisible to every other tenant. Manage tenants and keys with `ragbridge-admin`:

```bash
uv run ragbridge-admin create-tenant --name acme        # new tenant + first key
uv run ragbridge-admin list-tenants                     # list all tenants
uv run ragbridge-admin create-key --tenant-id <id> --name second-key
uv run ragbridge-admin revoke-key --prefix rb_abcdefgh  # prefix from list output
```

A key is printed once, at creation time, and stored only as a SHA-256 hash - there
is no way to recover a lost key, only to revoke it and create a new one.

## MCP

ragbridge is also an [MCP](https://modelcontextprotocol.io) server, so a client such as
Claude Desktop can search a tenant's documents. It exposes three tools -
`search_documents` (whole chunks, for the client's own model to reason over), `ask` (a
finished answer with sources), and `list_documents` - and deliberately not the agent: an
MCP client is already an agent and can call `search_documents` repeatedly itself.

**Over HTTP**, the server is mounted at `/mcp` (streamable HTTP) and takes the same
`Authorization: Bearer <key>` as every other endpoint. A request with no bearer token
gets `401`.

**Over stdio**, for desktop clients, run `ragbridge-mcp`. It is a thin proxy to a
*running* ragbridge, so it needs no database or Redis of its own:

```bash
RAGBRIDGE_API_KEY=<key> RAGBRIDGE_BASE_URL=http://localhost:8000 uv run ragbridge-mcp
```

`RAGBRIDGE_API_KEY` is required; `RAGBRIDGE_BASE_URL` defaults to
`http://localhost:8000`. In Claude Desktop's config:

```json
{"mcpServers": {"ragbridge": {
  "command": "uv",
  "args": ["--directory", "/path/to/ragbridge", "run", "ragbridge-mcp"],
  "env": {"RAGBRIDGE_API_KEY": "<key>"}
}}}
```

Both transports send every tool call through the REST API with the caller's own key, so
tenant isolation applies exactly as it does everywhere else - see
[ADR 0007](docs/adr/0007-mcp-server-on-the-mcp-sdk-2x.md).

## Deploying

For a real server, use the production Compose file, not the development one. It
publishes only a TLS proxy, refuses to start without its secrets, and runs the app as an
unprivileged user:

```bash
cp .env.prod.example .env.prod     # fill in POSTGRES_PASSWORD and REDIS_PASSWORD
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

Sizing, TLS and domains, choosing models, backups and restore, upgrades, security notes
and known limitations are in [docs/deployment.md](docs/deployment.md). A 21-check smoke
test of this stack (`scripts/smoke-prod.sh`) runs in CI. On a 2 vCPU / 4 GB server, run
embeddings locally and use a hosted chat model: measured, a local chat model as well
leaves almost no memory headroom.

## Configuration

All configuration is environment variables - see [.env.example](.env.example) for
the full list and defaults. The ones that most affect answer quality and behaviour:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge` | PostgreSQL connection (compose overrides it for the containers) |
| `MAX_UPLOAD_SIZE` | `10000000` | Largest accepted upload in bytes; bigger files get `413` |
| `ENVIRONMENT` | `development` | `development` or `production`. In production the app refuses to start with the database credentials published in `.env.example` |
| `ENABLE_DOCS` | `true` | Serve `/docs`, `/redoc` and `/openapi.json`; the production Compose file turns it off |
| `ENABLE_PLAYGROUND` | `true` | Serve the playground page at `/playground`; the production Compose file turns it off |
| `EMBEDDING_MODEL` | `ollama/nomic-embed-text` | LiteLLM model used to embed chunks |
| `EMBEDDING_DIMENSION` | `768` | Must match the embedding model's output size |
| `CHAT_MODEL` | `ollama/llama3.2` | LiteLLM model used to answer questions |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where to reach Ollama |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `200` | Chunking parameters |
| `RETRIEVAL_MODE` | `hybrid` | `hybrid` (vector + keyword, merged with reciprocal rank fusion), `vector`, or `keyword` |
| `RETRIEVAL_CANDIDATES` | `20` | Rows each retrieval arm contributes before fusion/reranking |
| `RERANK_ENABLED` | `false` | Whether `POST /query` reranks retrieved chunks before answering |
| `RERANK_MODEL` | `cohere/rerank-v3.5` | LiteLLM rerank model, used only when `RERANK_ENABLED=true` |
| `REDIS_URL` | `redis://localhost:6379/0` | Queue (background jobs) and cache connection |
| `ASYNC_PROCESSING_THRESHOLD` | `100000` | Uploads larger than this many bytes are processed by the worker |
| `EMBEDDING_CACHE_TTL` | `86400` | Seconds a cached embedding lives; `0` disables the embedding cache |
| `ANSWER_CACHE_ENABLED` | `false` | Whether `POST /query` caches whole answers |
| `ANSWER_CACHE_TTL` | `3600` | Seconds a cached answer lives, when enabled |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | *(unset)* | Set both to enable Langfuse tracing and cost tracking |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Point at a self-hosted Langfuse instance instead |
| `AGENT_MAX_STEPS` | `3` | Hard ceiling on searches per `POST /agent` call |
| `AGENT_PLANNER_MODEL` | *(empty)* | LiteLLM model that decides what to search next; empty reuses `CHAT_MODEL` |

To use a hosted provider instead of Ollama, change `EMBEDDING_MODEL` / `CHAT_MODEL`
to any [LiteLLM model name](https://docs.litellm.ai/docs/providers) (for example
`voyage/voyage-3` or `anthropic/claude-...`) and set the matching API key as an
environment variable. `EMBEDDING_DIMENSION` and `EMBEDDING_MODEL` are fixed per
installation: changing either after documents have been uploaded requires a new
migration and re-embedding every existing chunk.

Redis is required for background job processing and for caching; both features stay
inert (no connection attempted) until an upload actually crosses
`ASYNC_PROCESSING_THRESHOLD`, an embedding is actually requested, or the answer
cache is turned on - see [docs/plans/phase-3.md](docs/plans/phase-3.md), step 4, for
why. Langfuse is off unless both keys above are set - see
[ADR 0005](docs/adr/0005-cost-tracking-and-tracing-with-langfuse.md) for a known
limitation with the current LiteLLM/Langfuse SDK combination.

## Evaluation

`evaluation/` has a small hand-written corpus and question set, plus two scripts:
retrieval quality (recall@k, MRR - needs only an embedder) and answer quality
(RAGAS faithfulness / answer relevancy / context precision / context recall - needs
a real judge LLM). Both run by hand against a live server, never in CI, and both
need `--api-key` (an API key from `ragbridge-admin`) since every endpoint requires
one - see [docs/evaluation.md](docs/evaluation.md) for how to run them and the
latest results.

```bash
docker compose up -d
uv run ragbridge-admin create-tenant --name eval
uv run python -m evaluation.evaluate_retrieval --api-key <key>
uv run python -m evaluation.evaluate_answers --api-key <key>
```

## Development commands

```bash
uv run pytest              # run tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type check
scripts/smoke-prod.sh       # build and smoke-test the production stack (needs Docker)
```

Tests run against a real PostgreSQL with pgvector (`docker compose up -d postgres`),
in a **separate database**: `DATABASE_URL`'s database name plus `_test` (so `ragbridge`
becomes `ragbridge_test`), created and migrated automatically on the first run. The suite
empties every table before each test, so it refuses to run against any database whose name
does not end in `_test` - **your development data is never touched.** Tests never call a
real embedding provider, chat provider, queue, cache, or tracing
backend - see decision 5 in [docs/plans/phase-1.md](docs/plans/phase-1.md).

## License

MIT, see [LICENSE](LICENSE).
