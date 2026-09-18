# 3. Hybrid search with Reciprocal Rank Fusion

Date: 2026-09-18

## Status

Accepted

## Context

`POST /query` (Phase 1) retrieves chunks with a single method: nearest neighbours by
cosine distance over the embedding column, using pgvector's HNSW index. This works
well for paraphrases and synonyms - an embedding puts "how do I cancel?" close to
"terminating your subscription" - but it is weak on rare, literal tokens: an error
code, a product SKU, a person's name. An embedding model compresses a chunk into a
few hundred floats, and a token that carries no meaning derivable from its parts (for
example `ERR_4021`) tends to get blurred into "nearby meaning" rather than matched
exactly. A test in `tests/test_retrieval.py` demonstrates this directly: a chunk
containing an error code is not the top vector match for a query naming that same
code, because `FakeEmbedder` (and, in practice, most embedding models) have no way to
connect the literal string to the question without having seen it in a similar
context before.

PostgreSQL's own full-text search (`tsvector`, `to_tsquery`/`websearch_to_tsquery`,
`ts_rank`) has the opposite shape: it matches tokens, not meaning, so it finds the
error code reliably but misses the paraphrase. Neither method is a strict
improvement on the other - the fix is to run both and merge the results.

The question is how to merge two ranked lists whose scores are not on a comparable
scale: cosine similarity is a value roughly in [0, 1] with a narrow effective range
for most corpora (say 0.7-0.95), while `ts_rank` is an unbounded, corpus-dependent
number, often under 0.3. Any fixed weight applied to blend them directly (for example
`0.6 * similarity + 0.4 * ts_rank`) would need tuning per corpus and per embedding
model, and would silently go stale as either changes.

## Decision

Merge the two arms with Reciprocal Rank Fusion (RRF): each item's fused score is
`sum(1 / (k + rank))` over every ranking it appears in, using only its **position**
in each ranking (`rank`, 1-based), never the arm's own score. `k = 60`, the value
used in the original RRF paper and the default in most implementations.

Implementation: `vector_search` and `keyword_search` (`src/ragbridge/retrieval.py`)
each return up to `RETRIEVAL_CANDIDATES` rows (default 20, larger than a request's
`top_k` on purpose - narrowing a wide candidate set down is the fusion step's job,
and later the reranker's, not the arms'). `reciprocal_rank_fusion` is a pure function
merging their results; `hybrid_search` composes retrieval and fusion behind a
`RETRIEVAL_MODE` setting (`hybrid` by default, `vector` or `keyword` to disable
fusion and use one arm directly). See `docs/plans/phase-2.md` for the full step
breakdown and test list.

## Consequences

- RRF needs no score normalisation and no tuning - it only cares about rank
  position, so it stays correct as the embedding model, the corpus, or the
  full-text configuration change, none of which a fixed weight would survive
  unchanged.
- A chunk found by both arms, even at a modest rank in each, tends to outrank a
  chunk that is one arm's single favourite but invisible to the other - the
  concrete numbers in `tests/test_retrieval.py`'s
  `test_reciprocal_rank_fusion_favors_agreement_over_a_single_arms_top_pick` make
  this explicit: rank 1 + rank 8 (`1/61 + 1/68 ≈ 0.0311`) loses to rank 3 + rank 3
  (`1/63 + 1/63 ≈ 0.0317`).
- `Source.score` in the `POST /query` response changed meaning as a result: it is
  the fused score, not `1 - cosine_distance`, from Phase 2 step 2 onward. Still
  "higher is better," but no longer a similarity, and not comparable to a Phase 1
  score. `mode: "vector"` in a request restores the old scoring for a single call.
- The two retrieval queries run sequentially against one `AsyncSession`, not
  concurrently: a session allows only one query in flight at a time (it wraps a
  single database connection). Running them with `asyncio.gather` was the original
  plan and raises `sqlalchemy.exc.IllegalStateChangeError` - discovered while
  implementing step 2, corrected in the same commit.
- Reranking (Phase 2 step 3) sits on top of this: `hybrid_search` returns up to
  `RETRIEVAL_CANDIDATES` fused rows, and a `Reranker` (default: a no-op that only
  truncates to `top_k`) does the final narrowing, in every mode - including
  single-arm modes, a deliberate small efficiency trade for one shared code path.
