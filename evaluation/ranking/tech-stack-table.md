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
| Playground (Phase 6) | Plain HTML, CSS and JavaScript served by FastAPI; no build step, no npm |
