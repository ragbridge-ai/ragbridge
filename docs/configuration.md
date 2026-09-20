# Configuration

All configuration is environment variables - see [.env.example](../.env.example) for
the full list and defaults. The ones that most affect answer quality and behaviour:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge` | PostgreSQL connection (compose overrides it for the containers) |
| `MAX_UPLOAD_SIZE` | `10000000` | Largest accepted upload in bytes; bigger files get `413` |
| `PDF_EXTRACTION` | `auto` | How PDF pages are read: `auto` (row by row, so a side column of dates stays beside its text, but two columns of prose keep pypdf's order), `plain` (pypdf's default) or `layout` (always row by row). Applies to documents uploaded after a change; delete and re-upload a document to read it again |
| `ENVIRONMENT` | `development` | `development` or `production`. In production the app refuses to start with the database credentials published in `.env.example` |
| `ENABLE_DOCS` | `true` | Serve `/docs`, `/redoc` and `/openapi.json`; the production Compose file turns it off |
| `ENABLE_PLAYGROUND` | `true` | Serve the playground page at `/playground`; the production Compose file turns it off |
| `EMBEDDING_MODEL` | `ollama/nomic-embed-text` | LiteLLM model used to embed chunks |
| `EMBEDDING_DIMENSION` | `768` | Must match the embedding model's output size |
| `CHAT_MODEL` | `ollama/llama3.2` | LiteLLM model used to answer questions |
| `CHAT_TEMPERATURE` | `0` | Sampling temperature of the answer model (`0`-`2`). `0` gives the same answer to the same question over the same context; without it a provider uses its own default (Ollama: `0.8`) and a small local model can read one bullet under a different heading from run to run. Providers that reject the parameter do not receive it |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where to reach Ollama |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `200` | Maximum characters per chunk, and the context repeated between neighbours. Chunks are cut at line, sentence and word boundaries, never inside a word |
| `CHUNK_MIN_SIZE` | `100` | A chunk shorter than this is merged into its neighbour; `0` turns it off. A merged chunk can exceed `CHUNK_SIZE` by less than this. Independently of this setting, a Markdown heading line (`#` to `######`, then a space) is never the last line of a chunk: it moves to the start of the next one, so it stays with its text, and that chunk can exceed `CHUNK_SIZE` by the heading's length (a `# comment` in a code block looks the same and is treated the same). Chunks are built at upload: documents uploaded earlier keep their old chunks until you delete and re-upload them |
| `RETRIEVAL_MODE` | `hybrid` | `hybrid` (vector + keyword, merged with reciprocal rank fusion), `vector`, or `keyword` |
| `RETRIEVAL_CANDIDATES` | `20` | Rows each retrieval arm contributes before fusion/reranking |
| `KEYWORD_MAX_TERM_FREQUENCY` | `0.5` | A word of a keyword query found in more than this share of a tenant's chunks (a product name, "project") is left out of it; `1.0` turns it off. Not applied below 20 chunks or to quoted phrases and `-word` |
| `KEYWORD_RARITY_WEIGHTING` | `true` | Rank the keyword search's OR fallback by how rare the matched words are (a rare word outweighs several common ones) instead of `ts_rank`; only from 20 chunks; `false` turns it off |
| `RERANK_ENABLED` | `false` | Whether `/query`, `/search` and `/agent` rerank the retrieved chunks |
| `RERANK_BACKEND` | `api` | `api` calls a hosted rerank endpoint (`RERANK_MODEL`); `chat` has the chat model rate each of the best `RERANK_CANDIDATES` chunks against the question, so it works with local Ollama (one model call per candidate; `/agent` reranks every search step) |
| `RERANK_MODEL` | `cohere/rerank-v3.5` | LiteLLM rerank model, used only with `RERANK_BACKEND=api` |
| `RERANK_CHAT_MODEL` | *(empty)* | LiteLLM model that rates chunks with `RERANK_BACKEND=chat`; empty reuses `CHAT_MODEL` |
| `RERANK_CANDIDATES` | `10` | How many of the best fused chunks the `chat` backend rates (`1`-`40`); chunks after them keep their order |
| `ANSWER_CONTEXT_NEIGHBOURS` | `1` | Chunks before and after each retrieved chunk that `POST /query` also gives the answer model, joined into continuous excerpts; `0` turns it off |
| `ANSWER_CONTEXT_MAX_CHARS` | `6000` | Ceiling on the neighbours added to the answer context (retrieved chunks are always kept); protects a local model's small context window |
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
cache is turned on - see [docs/plans/phase-3.md](plans/phase-3.md), step 4, for
why. Langfuse is off unless both keys above are set - see
[ADR 0005](adr/0005-cost-tracking-and-tracing-with-langfuse.md) for a known
limitation with the current LiteLLM/Langfuse SDK combination.

**Changing a chunking setting does not rebuild existing documents.** Their chunks stay as
they were cut. To apply a change, delete the document and upload it again; there is no
re-ingest command.
