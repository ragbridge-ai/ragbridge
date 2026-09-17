# ragbridge

Open-source, ready-to-use RAG (Retrieval-Augmented Generation) service. It lets any
existing application (for example a Laravel, Symfony, or WordPress app) add chat and
smart search over its own data, without moving the application to Python.

## Status

**v0.1.0 — Basic RAG.** Upload text, Markdown, and PDF documents; they are chunked,
embedded, and stored in pgvector. `POST /query` retrieves the most relevant chunks
and answers using a chat model. See [AGENTS.md](AGENTS.md) for the full roadmap.

## Requirements

- Docker (runs PostgreSQL + pgvector, and the app itself)
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

The API is then available at `http://localhost:8000`, with a health check at
`http://localhost:8000/health`. Database migrations run automatically every time
the app container starts.

### Running without Docker

```bash
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn ragbridge.main:app --reload
```

## Usage

### Upload a document

```bash
curl -F "file=@notes.md;type=text/markdown" http://localhost:8000/documents
```

Text (`text/plain`), Markdown (`text/markdown`), and PDF (`application/pdf`) are
supported. Uploading the same content twice returns the existing document instead
of creating a duplicate.

### Ask a question

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What does this document say?", "top_k": 5}'
```

Returns `{"answer": "...", "sources": [...]}`. Each source reports the originating
document, its chunk position, a text snippet, and a relevance score.

### List and delete documents

```bash
curl http://localhost:8000/documents
curl -X DELETE http://localhost:8000/documents/<id>
```

## Configuration

All configuration is environment variables - see [.env.example](.env.example) for
the full list and defaults. The ones that most affect answer quality:

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | `ollama/nomic-embed-text` | LiteLLM model used to embed chunks |
| `EMBEDDING_DIMENSION` | `768` | Must match the embedding model's output size |
| `CHAT_MODEL` | `ollama/llama3.2` | LiteLLM model used to answer questions |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where to reach Ollama |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `200` | Chunking parameters |

To use a hosted provider instead of Ollama, change `EMBEDDING_MODEL` / `CHAT_MODEL`
to any [LiteLLM model name](https://docs.litellm.ai/docs/providers) (for example
`voyage/voyage-3` or `anthropic/claude-...`) and set the matching API key as an
environment variable. `EMBEDDING_DIMENSION` and `EMBEDDING_MODEL` are fixed per
installation: changing either after documents have been uploaded requires a new
migration and re-embedding every existing chunk.

## Development commands

```bash
uv run pytest              # run tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type check
```

Tests run against a real PostgreSQL with pgvector (`docker compose up -d postgres`)
and never call a real embedding or chat provider - see decision 5 in
[docs/plans/phase-1.md](docs/plans/phase-1.md).

## License

MIT, see [LICENSE](LICENSE).
