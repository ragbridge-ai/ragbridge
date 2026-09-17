# Phase 1 — Basic RAG

This document plans Phase 1 from the [roadmap in AGENTS.md](../../AGENTS.md#4-roadmap).
It records the decisions, the data model, the API endpoints, and the steps used to
build them.

## Decisions

1. **Async from the start**: async SQLAlchemy 2.x with psycopg 3 (`psycopg[binary]`).
   A RAG service mostly waits on network I/O (embedding API, LLM, database);
   switching from sync to async later would touch every layer. One driver serves
   the app, Alembic, and pgvector.
2. **Embedding model and dimension are settings**, fixed per installation. The
   pgvector column size comes from the setting. Changing the model or dimension
   requires a new migration and re-embedding all documents.
   - Default for local development: `ollama/nomic-embed-text` for embeddings
     (dimension 768) and an Ollama chat model (for example `ollama/llama3.2`)
     for answers. Ollama needs no API key.
   - Hosted providers are available through LiteLLM, by configuration only:
     Voyage AI for embeddings, Anthropic Claude or OpenAI for chat. Anthropic
     has no embedding model of its own.
   - Planned settings, implemented in step 4: `EMBEDDING_MODEL`,
     `EMBEDDING_DIMENSION`, `CHAT_MODEL`, `OLLAMA_BASE_URL`
     (default `http://localhost:11434`).
3. **Own small chunker** (paragraph-first, then character split, with size and
   overlap). No LangChain.
4. **PDF parsing with `pypdf`** (BSD), not PyMuPDF (AGPL does not fit an MIT
   project).
5. **Tests use real PostgreSQL with pgvector** (SQLite has no pgvector).
   Embeddings and chat sit behind a `typing.Protocol`. Tests and CI never call a
   real provider (Ollama or hosted); they use the fake implementation, so CI
   never needs an API key or a running Ollama instance.

## Data model

- `documents`: `id` (UUID), `filename`, `content_type`, `sha256` (unique),
  `created_at`
- `chunks`: `id` (UUID), `document_id` (FK, `ON DELETE CASCADE`), `chunk_index`,
  `content`, `embedding` (`vector(N)`), `metadata` (`jsonb`, e.g. PDF page number)
- HNSW index on `chunks.embedding`, cosine distance

## Endpoints

- `POST /documents` — multipart upload; parse, chunk, embed, store in the
  request (background jobs come in Phase 3); max upload size setting
- `GET /documents`, `DELETE /documents/{id}`
- `POST /query` — `{question, top_k}` → `{answer, sources[]}`; each source has
  `document_id`, `filename`, `chunk_index`, `snippet`, `score`

## Steps

1. Database foundation
2. Documents model + upload for text and Markdown (no embeddings)
3. PDF parsing + chunker (pure functions, unit tests)
4. Embeddings Protocol + LiteLLM implementation + fake; store chunk vectors
5. Vector retrieval + `POST /query` with answer and sources
6. Dockerfile (uv) + app service in docker-compose running migrations on start;
   README; ready for `v0.1.0`

Each step is built as several small, focused commits on its own feature branch.
