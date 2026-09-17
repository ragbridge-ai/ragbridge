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
  `content` (text, not null - the raw uploaded text, kept so documents can be
  re-chunked later without re-uploading), `created_at`
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

## Step 2 detail — documents model and upload

Branch: `feat/phase-1-documents-model` (created from `main`, off the merged step 1).
Step 1 (database foundation) is merged. Proposed commit breakdown:

1. **`feat(db): add documents model and migration`**
   - `Document` ORM model: `id` (UUID, PK), `filename` (str), `content_type` (str),
     `sha256` (str, unique), `content` (text, not null), `created_at` (timestamptz,
     server default `now()`).
   - Alembic migration creating the `documents` table with a unique index on
     `sha256`.
   - Test: create a `Document` row through the session, read it back.
2. **`feat(api): add POST /documents for text and Markdown`**
   - Multipart upload, restricted to `text/plain` and `text/markdown` (other
     content types get `415`).
   - `sha256` computed from the uploaded content; a second upload of the same
     content returns the existing document instead of erroring (idempotent).
   - Enforces a new `max_upload_size` setting.
   - Response: document id, filename, content_type, created_at.
   - Tests: successful upload, duplicate upload, oversized upload, unsupported
     content type.
3. **`feat(api): add GET /documents and DELETE /documents/{id}`**
   - `GET /documents` — list, newest first.
   - `DELETE /documents/{id}` — `204` on success, `404` if missing.
   - Tests for both, including the `404` case.

**Decided:** `documents` gets a `content TEXT NOT NULL` column now, storing the
raw uploaded text. No `chunks` table exists yet (that arrives once embeddings
do, in step 4 - see below), so without this column the uploaded content would
otherwise be lost; storing it means documents can be re-chunked later without
re-uploading.

## Step 3 detail — PDF parsing and chunker

Branch: `feat/phase-1-pdf-chunker` (created from `main`, off the merged step 2).
Step 2 (documents model and upload) is merged. Both pieces built here are pure
functions with unit tests only - no database or API changes yet. The upload
endpoint starts using them in step 4, once the `chunks` table exists to store
the result (the `embedding vector(N)` column needs `EMBEDDING_DIMENSION`,
which is a step 4 setting - see decision 2). Proposed commit breakdown:

1. **`feat(chunking): add paragraph-then-character chunker`**
   - `chunk_text(text, *, chunk_size, chunk_overlap) -> list[str]` in a new
     `src/ragbridge/chunking.py`. Splits on blank-line paragraph boundaries
     first; a paragraph longer than `chunk_size` is further split by
     characters, with `chunk_overlap` characters repeated between consecutive
     chunks so a fact split across a chunk boundary is not lost entirely.
   - Raises `ValueError` if `chunk_overlap >= chunk_size` (would loop forever
     or never advance).
   - Unit tests: empty text, short text (one chunk), several short paragraphs
     (one chunk per paragraph, no split), one long paragraph (character split
     with overlap), invalid `chunk_overlap`.
2. **`feat(pdf): add PDF text extraction`**
   - `extract_pdf_pages(data: bytes) -> list[str]` in a new
     `src/ragbridge/pdf.py`, using `pypdf.PdfReader`. Returns one string per
     page (not one joined string), so a later step can record which page a
     chunk came from - the `chunks.metadata` column already has this in mind
     (see Data model).
   - Add `pypdf` as a runtime dependency.
   - Add `fpdf2` as a **dev-only** dependency, used only to generate a real
     small PDF in a pytest fixture (two pages, known text). Reading a PDF that
     was actually built with a normal PDF library is a better test than
     hand-written PDF byte literals, which are brittle and unreadable in a
     diff.
   - Unit tests: two-page PDF -> two strings with the expected text; a page
     with no text -> empty string, not a crash; invalid PDF bytes -> a clear
     error (not a bare exception from `pypdf`).

**Decided:** neither function touches `POST /documents` yet. Wiring PDF
upload and chunking into the endpoint happens in step 4, together with
embeddings, because that is when the `chunks` table (and its `metadata`
column for the page number) is created - storing chunks before that table
exists would mean throwing the page numbers away and recomputing them later.

## Step 5 detail — vector retrieval and POST /query

Branch: `feat/phase-1-query` (created from `main`, off the merged step 4).
Step 4 (chunks, embeddings, upload wiring) is merged. Proposed commit
breakdown:

1. **`feat(chat): add Chatter protocol with LiteLLM and fake implementations`**
   - `Chatter` `Protocol` in a new `src/ragbridge/chat.py`, mirroring
     `Embedder` from step 4: `async def answer(self, question: str, context:
     list[str]) -> str`. The implementation owns prompt construction (a
     system prompt telling the model to answer only from the given context
     and say it does not know otherwise), so there is one place to change
     the wording later - the caller only supplies raw ingredients.
   - `LiteLLMChatter`: calls `litellm.acompletion` with `settings.chat_model`,
     passing `OLLAMA_BASE_URL` only for `ollama/*` models (same reasoning as
     `LiteLLMEmbedder` in step 4).
   - `FakeChatter`: deterministic, no network call, used by tests (decision 5).
   - `get_chatter` FastAPI dependency, overridable in tests the same way
     `get_embedder` is.
2. **`feat(api): add POST /query with vector retrieval`**
   - New router `src/ragbridge/api/query.py`. Request: `{question: str,
     top_k: int}` (`top_k` defaults to 5, capped at 20 via Pydantic `Field`
     validation - a request-level knob, not a new setting; nothing yet
     shows it needs to be fixed per installation).
   - Embeds the question with the injected `Embedder`, then selects the
     `top_k` nearest chunks by `Chunk.embedding.cosine_distance(...)`
     (matches the HNSW index's `vector_cosine_ops` from step 4), joined
     with their `Document` for `filename`.
   - Calls `Chatter.answer(question, [chunk.content for chunk in ...])` and
     returns `{answer, sources[]}`, each source: `document_id`, `filename`,
     `chunk_index`, `snippet` (chunk content truncated to a fixed length -
     the full content is never returned by the API today either), `score`
     (`1 - cosine_distance`, so higher means more relevant).
   - Test fixture: override `get_chatter` with `FakeChatter`, same pattern
     as `get_embedder`.
   - Tests: a question whose text exactly matches an uploaded chunk's
     content ranks that chunk first with `score` 1.0 (`FakeEmbedder` is
     deterministic per exact text, so identical text embeds identically -
     this is how retrieval order is tested meaningfully without a real,
     semantically-aware embedder); `top_k` limits the number of sources;
     an empty database returns `sources: []` and still calls `Chatter`
     (with empty context, not skipped) so the "I don't know" behavior is
     the model's responsibility, not a special case in the endpoint.
