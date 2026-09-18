# ragbridge

Open-source, ready-to-use RAG (Retrieval-Augmented Generation) service. It lets any
existing application (for example a Laravel, Symfony, or WordPress app) add chat and
smart search over its own data, without moving the application to Python.

## Status

**v0.2.0 — Production-ready.** Every endpoint except `/health` requires an API key;
each key belongs to a tenant, and tenants never see each other's documents. Large
uploads are processed by a background worker instead of blocking the request.
Embeddings are cached by default, whole answers can optionally be cached too, and
Langfuse tracing/cost tracking is available. See [AGENTS.md](AGENTS.md) for the full
roadmap and [docs/plans/phase-3.md](docs/plans/phase-3.md) for the decisions behind
this phase.

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
available at `http://localhost:8000`, with a health check at
`http://localhost:8000/health` (the only endpoint that needs no API key). Database
migrations run automatically every time the app container starts.

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

### List, fetch, and delete documents

```bash
curl -H "Authorization: Bearer <key>" http://localhost:8000/documents
curl -H "Authorization: Bearer <key>" http://localhost:8000/documents/<id>
curl -H "Authorization: Bearer <key>" -X DELETE http://localhost:8000/documents/<id>
```

## Multi-tenancy and API keys

Every request except `GET /health` needs `Authorization: Bearer <key>`; a key
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

## Configuration

All configuration is environment variables - see [.env.example](.env.example) for
the full list and defaults. The ones that most affect answer quality and behaviour:

| Variable | Default | Purpose |
|---|---|---|
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
```

Tests run against a real PostgreSQL with pgvector (`docker compose up -d postgres`)
and never call a real embedding provider, chat provider, queue, cache, or tracing
backend - see decision 5 in [docs/plans/phase-1.md](docs/plans/phase-1.md).

## License

MIT, see [LICENSE](LICENSE).
