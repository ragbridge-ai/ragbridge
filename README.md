# ragbridge

Open-source, ready-to-use RAG (Retrieval-Augmented Generation) service. It lets any
existing application (for example a Laravel, Symfony, or WordPress app) add chat and
smart search over its own data, without moving the application to Python.

## Status

**Phase 0 — Setup.** Project skeleton only: FastAPI app factory, `/health` endpoint,
tests, linting, type checking, Docker Compose for PostgreSQL + pgvector, and CI.
No RAG functionality yet — see [AGENTS.md](AGENTS.md) for the full roadmap.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker (for local PostgreSQL + pgvector)

## Getting started

```bash
cp .env.example .env                        # local configuration
uv sync                                      # install dependencies
docker compose up -d                         # start PostgreSQL + pgvector
uv run uvicorn ragbridge.main:app --reload   # run the API locally
```

The API is then available at `http://localhost:8000`, with a health check at
`http://localhost:8000/health`.

## Development commands

```bash
uv run pytest              # run tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type check
```

## License

MIT, see [LICENSE](LICENSE).
