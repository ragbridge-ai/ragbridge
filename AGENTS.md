# AGENTS.md — ragbridge

This file gives AI coding assistants (Cursor, Claude Code, and others) the full context of this project.
Read it before making any change.

## 1. What this project is

`ragbridge` is an open-source, ready-to-use RAG (Retrieval-Augmented Generation) service.
It lets any existing application (for example a Laravel, Symfony, or WordPress app) add
chat and smart search over its own data, without moving the application to Python.

- GitHub organization: `ragbridge-ai`
- This repo: `ragbridge-ai/ragbridge` (Python service, the core of the project)
- Second repo (later): `ragbridge-ai/ragbridge-php` (PHP client, Composer package `ragbridge/php`)
- PyPI package name: `ragbridge`
- License: MIT

The goal is a project that real people can use, not only a portfolio demo.
Keep the scope focused: "RAG ready for existing applications". Do NOT try to build a general
framework like LangChain.

## 2. Tech stack

| Area | Tool |
|---|---|
| Language | Python 3.12 |
| Package and environment manager | `uv` (never use plain `pip` or `requirements.txt`) |
| Web framework | FastAPI + Uvicorn |
| Settings and validation | Pydantic, pydantic-settings |
| Database | PostgreSQL with `pgvector` |
| ORM and migrations | SQLAlchemy 2.x + Alembic (from Phase 1) |
| LLM access | LiteLLM (OpenAI, Anthropic, local models via Ollama) |
| Queue and cache | Redis (from Phase 3) |
| Agents | Own bounded loop, no framework (from Phase 4) |
| MCP | `mcp` SDK 2.x, `MCPServer` (from Phase 4) |
| Evaluation | RAGAS (from Phase 2) |
| Observability | Langfuse (from Phase 3) |
| Lint and format | ruff |
| Type checking | mypy (strict) |
| Tests | pytest, httpx |
| CI | GitHub Actions |
| Local infrastructure | Docker Compose |
| Deployment (Phase 5) | Docker Compose + Caddy on a single Linux host |

Only add a tool when the current phase needs it. Every important tool choice gets a short
Architecture Decision Record (ADR) in `docs/adr/`.

## 4. Roadmap

### Phase 0 — Setup (Week 1) ✅ Done
Clean project skeleton: `uv` project with src layout, FastAPI app factory, `/health` endpoint,
tests, ruff, mypy, Docker Compose with PostgreSQL + pgvector, GitHub Actions CI, README, LICENSE.
Output: public repo with green CI.

### Phase 1 — Basic RAG (Weeks 2–4)
Upload documents (text, Markdown, PDF), chunking, embeddings, storage in pgvector,
`/query` endpoint that returns an answer with sources, LiteLLM, SQLAlchemy + Alembic.
Output: `v0.1.0`, runs with `docker compose up`.
See the detailed plan: [docs/plans/phase-1.md](docs/plans/phase-1.md).

### Phase 2 — Quality (Weeks 5–6)
Hybrid search (pgvector + PostgreSQL full-text search), reranking, test dataset,
RAGAS evaluation with scores in the README.
See the detailed plan: [docs/plans/phase-2.md](docs/plans/phase-2.md).

### Phase 3 — Production-ready (Weeks 7–8)
API key authentication, multi-tenancy, background jobs for large files (Redis + worker),
caching, tracing and cost tracking with Langfuse.
Output: `v0.2.0`.
See the detailed plan: [docs/plans/phase-3.md](docs/plans/phase-3.md).

### Phase 4 — Agents and MCP (Weeks 9–10)
Multi-step agent mode (own bounded loop - LangGraph was evaluated and rejected,
see the plan's decision 1), MCP server exposing search over a tenant's documents.
Output: `v0.3.0`.
See the detailed plan: [docs/plans/phase-4.md](docs/plans/phase-4.md).

### Phase 5 — Deploy and release (Weeks 11–12)
Self-hosted deployment on a single Linux host (hardened image, production Compose file
with a Caddy TLS proxy, production-safe settings), full docs, ADRs, demo in README.
AWS + Terraform was evaluated and dropped - see the plan's decision 1.
Output: `v1.0.0`.
See the detailed plan: [docs/plans/phase-5.md](docs/plans/phase-5.md).

### After v1
GraphRAG with Neo4j, Qdrant adapter behind the same interface as pgvector, direct MySQL
sync, and Terraform for a cloud provider (deferred from Phase 5: it cannot be applied,
or even planned, without an account).

## 5. Project structure (target)

```
ragbridge/
├── .github/workflows/ci.yml
├── docs/
│   └── adr/
├── src/ragbridge/
│   ├── __init__.py
│   ├── main.py            # app factory: create_app()
│   ├── config.py          # settings with pydantic-settings
│   └── api/
│       ├── __init__.py
│       └── health.py
├── tests/
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── LICENSE
├── AGENTS.md
├── CLAUDE.md
└── README.md
```

## 6. Coding rules

- Use the src layout. All application code lives in `src/ragbridge/`.
- Use the application factory pattern (`create_app()`), so tests can build a fresh app.
- Full type hints everywhere. Code must pass `mypy --strict`.
- Code must pass `ruff check` and `ruff format --check`.
- Every new endpoint or feature needs tests.
- Configuration comes from environment variables through pydantic-settings. Never hard-code
  secrets. Keep `.env.example` up to date; never commit `.env`.
- Keep functions small and names clear. Prefer simple code over clever code.
- Use Conventional Commits for commit messages (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).

## 7. Commands

```bash
uv sync                                   # install dependencies
uv run pytest                             # run tests
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run mypy                                # type check
uv run uvicorn ragbridge.main:app --reload  # run the API locally
docker compose up -d                      # start PostgreSQL + pgvector
```

## 8. How to work on tasks

1. Work on one phase or one step at a time. Do not jump ahead to later phases.
2. Before writing code, give a short plan and wait for approval if the task is large.
3. After changes, run tests, ruff, and mypy, and make sure they pass.
4. At the end, summarize what you changed, why, and what the maintainer should learn from it.
5. Never commit to `main` directly. Work on one feature branch per step and push
   only that branch. Never merge, push to `main`, force-push, create tags,
   publish packages, or change repository settings. The maintainer opens and
   merges pull requests.
