# Phase 2 — Quality

This document plans Phase 2 from the [roadmap in AGENTS.md](../../AGENTS.md#4-roadmap).
Phase 1 built a RAG pipeline that works. Phase 2 makes its retrieval better and,
for the first time, measurable.

Today `POST /query` embeds the question and takes the `top_k` nearest chunks by
cosine distance. That single arm is good at meaning ("how do I cancel?" finds
"terminating your subscription") and weak at exact tokens (error codes, product
names, `RAGBRIDGE_API_KEY`). Phase 2 adds a second retrieval arm, a precise
re-scoring pass on top of both, and a dataset that turns "it feels better" into
a number.

## Decisions

1. **Hybrid search = vector + PostgreSQL full-text search, merged with
   Reciprocal Rank Fusion (RRF).** The two arms fail in opposite ways: vector
   search misses rare literal tokens because an embedding blurs them into
   nearby meaning; full-text search misses paraphrases because it matches
   words, not ideas. Running both and merging keeps what either one alone
   would drop.
   RRF merges by **rank**, not by score: `score = sum over arms of
   1 / (k + rank)`, with `k = 60` (the value from the original RRF paper, and
   the default in most implementations). Rejected alternative: normalising both
   scores and taking a weighted sum. A cosine similarity of `0.82` and a
   `ts_rank` of `0.19` do not live on the same scale, and their distributions
   change per corpus and per query, so any fixed weight is a guess that has to
   be retuned. RRF needs no tuning and no score normalisation at all.
2. **The `tsvector` is a generated stored column, with the text search
   configuration `english` hard-coded.** A `GENERATED ALWAYS AS ... STORED`
   column is kept in sync by PostgreSQL itself: no trigger to write, and no
   application code that can forget to update it when a chunk is inserted.
   The configuration is **not** a setting, unlike `EMBEDDING_DIMENSION`. If the
   index is built with one configuration and the query uses another, PostgreSQL
   does not complain - it just returns worse results, silently. A dimension
   mismatch fails loudly; a language mismatch does not. Multi-language corpora
   are a post-v1 topic (they need per-document language detection anyway, not
   one global switch).
3. **Reranking sits behind a `Protocol`**, exactly like `Embedder` (Phase 1
   step 4) and `Chatter` (step 5). The real implementation calls a hosted
   reranking model through LiteLLM (Cohere, Voyage, or Jina - all support the
   same rerank API shape). The **default is a no-op reranker**, so the
   key-free `docker compose up` path with local Ollama keeps working: Ollama
   has no rerank endpoint, and a local cross-encoder would pull in torch
   (~2 GB), slow down CI, and bloat the Docker image for a feature most
   installations will not switch on.
4. **The evaluation corpus is written by hand**: a handful of short Markdown
   documents for an invented company (HR policy, refund rules, API error
   codes, onboarding). No licensing question, and - more important - we
   control the content, so every question in the dataset has exactly one
   correct source chunk. Recall@k is only trustworthy when "the right chunk"
   is not a matter of opinion. Rejected: this repo's own docs (they change
   with every commit, so scores stop being comparable) and public-domain
   prose (harder to write unambiguous questions against).
5. **Evaluation runs by hand; CI only smoke-tests it.** Real scores need a
   real embedder and, for RAGAS, a judge LLM. CI must stay key-free and fast
   (Phase 1 decision 5), so the maintainer runs the evaluation locally and
   commits the resulting table. CI runs the same script against the fakes,
   which proves the script still works without measuring anything - enough to
   stop it rotting unnoticed.

## Data model changes

- `chunks` gains `content_tsv tsvector GENERATED ALWAYS AS
  (to_tsvector('english', content)) STORED`, plus a GIN index on it.
  In SQLAlchemy: `mapped_column(TSVECTOR, Computed("to_tsvector('english',
  content)", persisted=True))`.
- No other table changes in this phase.

## API changes

- `POST /query` accepts an optional `mode` field (`hybrid` | `vector` |
  `keyword`). Without it, the request uses the `RETRIEVAL_MODE` setting. This
  exists so step 4 can evaluate the same dataset under each mode in one run,
  without editing `.env` between runs.
- **`Source.score` changes meaning.** In Phase 1 it was `1 - cosine_distance`.
  From step 2 on it is the final ranking score: the RRF score, or the
  reranker's score when reranking is on. It stays "higher is better", but the
  numbers are no longer comparable to Phase 1 values and are not a similarity.

## New settings

| Setting | Default | Meaning |
|---|---|---|
| `RETRIEVAL_MODE` | `hybrid` | Which arms to use: `hybrid`, `vector`, `keyword` |
| `RETRIEVAL_CANDIDATES` | `20` | Rows each arm returns before fusion |
| `RERANK_ENABLED` | `false` | Whether to rerank the fused candidates |
| `RERANK_MODEL` | `cohere/rerank-v3.5` | LiteLLM rerank model, used only when enabled |

`RETRIEVAL_CANDIDATES` is deliberately larger than `top_k`: the two cheap arms
cast a wide net, and the expensive, accurate step (fusion, then reranking)
narrows it down to `top_k`. Retrieving only `top_k` per arm would mean the
reranker never sees the chunk it was supposed to rescue.

## Steps

1. Full-text search arm: `tsvector` column, GIN index, `retrieval.py` with
   `vector_search` and `keyword_search`
2. Hybrid retrieval: RRF as a pure function, retrieval settings, wired into
   `POST /query`
3. Reranking: `Reranker` protocol, LiteLLM and no-op and fake implementations
4. Evaluation corpus and dataset; retrieval metrics (recall@k, MRR) - no LLM
   judge needed
5. RAGAS answer-quality metrics, `docs/evaluation.md`, scores in the README,
   ADRs

Each step is built as several small, focused commits on its own feature branch.

## Step 1 detail — full-text search arm

Branch: `feat/phase-2-fts` (created from `main`, off the merged Phase 1).
This step adds the second retrieval arm and gives it a home, but does **not**
change what `POST /query` returns yet - fusion arrives in step 2. Proposed
commit breakdown:

1. **`docs: add Phase 2 plan`** (this document).
2. **`feat(db): add full-text search column and index to chunks`**
   - `Chunk.content_tsv` as a `Computed` column (decision 2) and an Alembic
     migration creating it together with a GIN index. GIN, not GiST: GIN is
     slower to build and faster to search, and a RAG corpus is written rarely
     and searched constantly.
   - The migration backfills nothing by hand - a generated column is computed
     for every existing row as it is added.
   - Test: insert a chunk, read `content_tsv` back, and confirm PostgreSQL
     filled it without the application setting it.
3. **`feat(retrieval): add vector and keyword search functions`**
   - New `src/ragbridge/retrieval.py`. Move the vector query out of
     `api/query.py` into `vector_search(session, embedding, limit)`, returning
     `(chunk, document, score)` rows. Pure refactor: `POST /query` behaves
     exactly as before, and its existing tests are the proof.
   - Add `keyword_search(session, query, limit)` using
     `websearch_to_tsquery('english', ...)` and `ts_rank`.
     `websearch_to_tsquery` is the parser that accepts what users actually
     type (bare words, `"quoted phrases"`, `or`, `-excluded`) and never raises
     on malformed input - `to_tsquery` would reject a plain sentence outright.
   - Tests: a chunk holding a rare literal token (an error code) is found by
     `keyword_search` and is *not* ranked first by `vector_search`, because
     `FakeEmbedder` has no semantics. That asymmetry is exactly the gap the
     second arm exists to close, so it is worth a test rather than a comment.
