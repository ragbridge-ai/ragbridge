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

## Step 2 detail — hybrid retrieval and POST /query

Branch: `feat/phase-2-hybrid` (created from `main`, off the merged step 1).
Step 1 (full-text search arm) is merged: `vector_search` and `keyword_search`
exist as two independent functions with the same `(chunk, document, score)`
shape, but nothing combines them yet, and `POST /query` still only calls
`vector_search`. Proposed commit breakdown:

1. **`docs: add Phase 2 step 2 plan`** (this section).
2. **`feat(retrieval): add reciprocal rank fusion`**
   - `reciprocal_rank_fusion(rankings, *, k=60) -> list[SearchResult]` in
     `retrieval.py`, a **pure function** - no session, no `await`, unit tested
     the same way `chunk_text` was (Phase 1 step 3). Each input ranking is one
     arm's results, best first; an item is identified by `chunk.id`, and its
     fused score is `sum(1 / (k + rank) for each ranking it appears in)`,
     `rank` 1-based. This uses only *position* in each ranking, never the
     arm's own score (decision 1: a cosine similarity and a `ts_rank` are not
     on comparable scales, so blending them directly would need a tuned,
     corpus-specific weight; rank position needs none).
   - Tests: a chunk ranked consistently in the middle by both arms outranks
     one arm's top pick that the other arm ranks far down (the concrete
     numbers from decision 1: rank 1 + rank 8 scores `1/61 + 1/68 ≈ 0.0311`,
     rank 3 + rank 3 scores `1/63 + 1/63 ≈ 0.0317` and wins); an item found by
     only one arm is still scored and included, from that one ranking alone;
     empty rankings return an empty list.
3. **`feat(config): add retrieval settings`**
   - `RETRIEVAL_MODE: Literal["hybrid", "vector", "keyword"] = "hybrid"` and
     `RETRIEVAL_CANDIDATES: int = 20` (how many rows each arm contributes
     before fusion - decision-adjacent: kept larger than `top_k` so a later
     reranker, step 3, has more than `top_k` candidates to actually rerank).
   - `.env.example` updated to document both, next to the existing chunking
     settings.
4. **`feat(api): wire hybrid retrieval into POST /query`**
   - `hybrid_search(session, embedding, query, *, mode, candidates, top_k)`
     in `retrieval.py`: runs `vector_search` then `keyword_search` when
     `mode == "hybrid"`, fuses with `reciprocal_rank_fusion`, and returns the
     first `top_k` rows; `mode == "vector"` or `"keyword"` runs only that one
     arm, unfused, so the setting can disable hybrid entirely without a
     second code path in the endpoint.
     **Corrected while implementing:** the two arms were planned to run
     concurrently with `asyncio.gather`, since they query independent
     indexes. They can't - both go through the same `AsyncSession`, and a
     session allows only one query in flight at a time (it wraps a single
     database connection, which is the actual constraint). They run one
     after another instead.
   - `QueryRequest` gains an optional `mode` field, defaulting to
     `settings.retrieval_mode` when absent (decision: this lets step 4's
     evaluation script compare modes against the same running server without
     restarting it with different settings).
   - **`Source.score` now means the fused score**, not `1 - cosine_distance`
     (flagged in advance in the Phase 2 "API changes" section above). Existing
     `test_query.py` assertions that hard-code `score == 1.0` for an exact
     match are updated: RRF's top score for an item found by every arm is
     `sum(1 / (k + 1))` over the arms that ran, not `1.0`.
   - Tests: a question matching one chunk's exact text and a second chunk's
     rare token both come back, in `hybrid` mode, ahead of where either arm
     alone would rank them; `mode="vector"` reproduces the pre-hybrid ranking;
     an unsupported `mode` value is rejected by Pydantic with `422`, not a
     runtime error.

## Step 3 detail — reranking

Branch: `feat/phase-2-rerank` (created from `main`, off the merged step 2).
Step 2 (hybrid retrieval) is merged: `hybrid_search` fuses both arms and
returns `top_k` rows, ready to display. Proposed commit breakdown:

1. **`docs: add Phase 2 step 3 plan`** (this section).
2. **`feat(config): add rerank settings`**
   - `RERANK_ENABLED: bool = False` and `RERANK_MODEL: str =
     "cohere/rerank-v3.5"`. `.env.example` updated.
   - **Reordered while implementing:** originally listed after the
     `Reranker` protocol commit. `get_reranker` (next commit) reads
     `settings.rerank_enabled` to choose an implementation, so the setting
     has to exist first - the same order Phase 1 used for `Embedder` and
     `Chatter` (`feat(config): add embedding, chat, and chunking settings`
     landed before either protocol), which this plan should have followed
     to begin with.
3. **`feat(rerank): add Reranker protocol with LiteLLM, no-op, and fake implementations`**
   - New `src/ragbridge/rerank.py`, the same shape as `Embedder` (Phase 1
     step 4) and `Chatter` (step 5): a `Reranker` `Protocol` with `async def
     rerank(self, query: str, candidates: list[SearchResult], top_k: int) ->
     list[SearchResult]`.
   - `NoOpReranker`: returns `candidates[:top_k]`, unchanged order - **the
     default** (decision 3). Ollama has no rerank endpoint, and a local
     cross-encoder means bundling torch (~2 GB) for a feature most installs
     will leave off, so `docker compose up` with no API key keeps reranking
     nothing until it is explicitly switched on.
   - `LiteLLMReranker`: calls `litellm.arerank(model=settings.rerank_model,
     query=query, documents=[chunk.content for chunk, _, _ in candidates],
     top_n=top_k)`. LiteLLM standardises the rerank response shape across
     Cohere, Voyage, and Jina, the same way it already standardises chat and
     embedding responses across providers.
   - `FakeReranker`: deterministic, no network call - reverses the given
     order before truncating, so a test can assert that reranking actually
     changed something, not merely that it ran (mirrors `FakeChatter`'s fixed
     reply, which exists for the same reason: something checkable that a real
     provider's response wouldn't be).
   - `get_reranker` FastAPI dependency: `NoOpReranker()` unless
     `settings.rerank_enabled`, then `LiteLLMReranker(settings)`.
   - Tests: `NoOpReranker` returns the first `top_k` candidates, order
     unchanged; `FakeReranker` returns the reversed order, truncated to
     `top_k`. Both pure - no session, no network call - unit tested directly.
4. **`feat(api): wire reranking into POST /query`**
   - **`hybrid_search`'s contract changes**: it now returns up to
     `candidates` rows, not `top_k` - narrowing to `top_k` becomes the
     reranker's job, always, even when reranking is off. `NoOpReranker`'s
     truncation *is* the `[:top_k]` slice `hybrid_search` used to do itself,
     moved one layer up so a real reranker can occupy exactly that spot.
   - **Call out now:** the single-arm modes (`"vector"`, `"keyword"`) also
     switch to fetching `candidates` rows instead of exactly `top_k`, even
     when reranking is off - a little more database work than step 2's
     version, in exchange for one code path shared by every mode instead of
     a fetch-size special case for "no reranker." `candidates` defaults to
     20, small enough that the extra cost is not worth a second path
     (AGENTS.md: prefer simple code over clever code).
   - `POST /query` takes the `Reranker` dependency and calls
     `reranker.rerank(request.question, rows, request.top_k)` after
     `hybrid_search`, in every mode - so `NoOpReranker` (the default) and a
     real reranker sit in exactly the same call site.
   - Tests: overriding `get_reranker` with `FakeReranker` and asserting the
     response comes back in the reversed candidate order proves the endpoint
     actually calls the reranker, not merely that retrieval's own order
     survives unchanged; with the default `NoOpReranker`
     (`RERANK_ENABLED=false`), every step 2 `POST /query` test keeps passing
     unmodified, because `NoOpReranker`'s truncation reproduces the old
     internal `[:top_k]` slice exactly.

## Step 4 detail — evaluation corpus and retrieval metrics

Branch: `feat/phase-2-eval-dataset` (created from `main`, off the merged step
3). Step 3 (reranking) is merged: `POST /query` now runs the full pipeline
(hybrid retrieval, optional reranking). This step measures it. No LLM judge
is needed yet - recall@k and MRR only need an embedder, which is what makes
them cheap enough to run on every retrieval change, unlike RAGAS (step 5).

**Design decision made while planning this step:** the evaluation script
drives everything through `POST /documents` and `POST /query` - the actual,
public HTTP contract - rather than importing `retrieval.py` functions
directly. A `Source` in the response already carries `snippet`, which is
enough to tell whether the retrieved chunk is the one a question is asking
about: a question's dataset entry gives a short, unique quote from its
source chunk, and "was the right chunk retrieved" becomes "does that quote
appear in one of the returned snippets." This means the exact same function
serves two purposes with no separate mock layer: point it at a real running
server (`httpx.Client(base_url=...)`) for a genuine evaluation, or at
`TestClient(app_with_database)` with `FakeEmbedder`/`FakeChatter` overrides
(decision 5) for the CI smoke test - `starlette.testclient.TestClient`
subclasses `httpx.Client`, so both satisfy the same type.

Proposed commit breakdown:

1. **`docs: add Phase 2 step 4 plan`** (this section).
2. **`feat(evaluation): add the corpus`**
   - `evaluation/corpus/`: six short Markdown documents for a fictional
     company, "Acme Cloud" - `hr-policy.md`, `refund-policy.md`,
     `api-error-codes.md`, `onboarding-guide.md`, `security-policy.md`,
     `billing-faq.md`. Each document is a `# Title` followed by several
     one-paragraph topics (`**Label.** Sentence. Sentence.`), no blank line
     inside a topic - so `chunk_text`'s paragraph split gives one chunk per
     topic, not one chunk per heading plus one per paragraph.
     `api-error-codes.md` is deliberately full of literal tokens (`ERR_1001`,
     `ERR_4021`, ...) - the same kind of content the step 1 test proved
     vector search struggles with, so the corpus can show the same gap at
     evaluation scale, not just in one hand-picked unit test.
   - No code in this commit - data only, matching decision 4 (a hand-written
     corpus, so every question has exactly one correct source chunk).
3. **`feat(evaluation): add the question dataset`**
   - `evaluation/dataset.jsonl`, 25 questions (one per targeted paragraph,
     spread across all six documents), each line
     `{"question": ..., "source_document": ..., "source_snippet": ...}`.
     `source_snippet` is a short, verbatim quote from the paragraph the
     question is about - unique enough in the corpus that finding it in a
     response's `sources[].snippet` reliably means "the right chunk came
     back," with no chunk id or document id bookkeeping needed.
4. **`feat(evaluation): add the retrieval evaluation script`**
   - New `evaluation/evaluate_retrieval.py`: `load_dataset()`,
     `upload_corpus(client)` (posts every corpus file to `/documents`), and
     `evaluate_retrieval(client, questions, *, top_k=5) -> EvaluationResult`
     (posts each question to `/query`, finds the first source, if any, whose
     `snippet` contains that question's `source_snippet`, and computes
     **recall@k** - the fraction of questions where the right chunk appears
     anywhere in the top `k` - and **MRR**, mean reciprocal rank, which
     additionally rewards the right chunk appearing *near the top*, not just
     somewhere in the list).
   - A `main()` builds a real `httpx.Client(base_url=...)` (default
     `http://localhost:8000`, overridable with `--base-url`), runs the
     evaluation against a running server, and prints recall@k and MRR - run
     by hand, per decision 5, since it needs a real embedder.
   - `evaluation` added to `pyproject.toml`'s `[tool.mypy] files`, so this
     script is held to the same `mypy --strict` standard as `src` and
     `tests` (AGENTS.md coding rules apply to every commit, not only
     application code).
   - New `tests/test_evaluate_retrieval.py`: the **CI smoke test** (decision
     5). Builds `TestClient(app_with_database)` with `FakeEmbedder` and
     `FakeChatter` overrides (the existing fixture), runs
     `upload_corpus` then `evaluate_retrieval` against it, and asserts only
     that it completes and returns a result of the right shape
     (`questions_evaluated == 25`, both metrics between 0.0 and 1.0) - never
     a specific score, because `FakeEmbedder` has no semantics and a
     meaningful score requires a real embedder. This is what stops the
     script from rotting unnoticed, without needing an API key in CI.

## Step 5 detail — answer quality with RAGAS

Branch: `feat/phase-2-ragas` (created from `main`, off the merged step 4).
Step 4 (recall@k, MRR) measures whether retrieval finds the right chunk.
This step measures the thing retrieval can't: whether the *generated
answer* is actually faithful to that chunk and relevant to the question -
which needs a judge LLM to assess, so it is always run by hand (decision 5),
never in CI.

**Everything below was verified against the actually-installed `ragas`
version (`0.4.3`) and a real local Ollama instance before being written into
this plan or any code** - RAGAS's Python API has changed enough across
versions that guessing from memory would have produced code that imports
cleanly and then fails at the first real call.

**Dependency issue found and fixed:** `ragas==0.4.3` fails at
`import ragas` with `ModuleNotFoundError: No module named
'langchain_community.chat_models.vertexai'` against the latest
`langchain-community` (`0.4.2`) - that submodule was removed as part of
`langchain-community`'s sunset, but `ragas` still imports it unconditionally.
Fix: pin `langchain-community<0.4` alongside `ragas` in the dev group,
with a comment in `pyproject.toml` explaining why. See ADR 0004.

**API design found while verifying:** `ragas`'s newer, non-deprecated
metric classes (`ragas.metrics.collections.{Faithfulness,AnswerRelevancy,
ContextPrecision,ContextRecall}`) require a "modern instructor-based" LLM
(`ragas.llms.llm_factory`, wrapping an `AsyncOpenAI`-shaped client) - they
explicitly reject the classic `LangchainLLMWrapper` path with a clear error.
Since Ollama serves an OpenAI-compatible API at `/v1`, `llm_factory` is
pointed at `AsyncOpenAI(base_url=f"{settings.ollama_base_url}/v1",
api_key="ollama")` by default - the same local, key-free judge setup the
rest of the app already assumes, no new provider needed. Embeddings use
`ragas.embeddings.LiteLLMEmbeddings`, matching `settings.embedding_model`.
The top-level `ragas.evaluate()` orchestrator does **not** yet accept these
newer metric objects (verified: it raises `TypeError`, "All metrics must be
initialised metric objects") - the script calls each metric's `.ascore(...)`
directly instead.

**A second, unrelated regression found because of this dependency, fixed
in its own commit:** `ragas` pulls in `langchain-core` → `langsmith`, which
depends on a package called `httpx2`. Once `httpx2` is importable,
`starlette.testclient.TestClient` switches its base class from
`httpx.Client` to `httpx2.Client` (a deprecation warning already visible in
every test run since Phase 1 - "install httpx2 instead" - was the advance
notice). This breaks `evaluate_retrieval.py`'s `client: httpx.Client` type
hint from step 4: `TestClient` is no longer nominally an `httpx.Client`,
though it is still behaviorally identical (the existing smoke test keeps
passing at runtime; only `mypy` catches it). Fixed by replacing the nominal
`httpx.Client` type hints in both evaluation scripts with a small structural
`Protocol` capturing only the `.post(...)` call they actually use - the same
"depend on shape, not on a concrete class" idiom this codebase already uses
for `Embedder`, `Chatter`, and `Reranker`, and more robust than assuming
which HTTP library `TestClient` happens to subclass this month.

**Dataset change:** each `evaluation/dataset.jsonl` line gains a
`reference_answer` field - a short, ground-truth answer, grounded in the
same corpus paragraph as `source_snippet`. `context_precision` and
`context_recall` need a reference to compare retrieved context against;
`faithfulness` and `answer_relevancy` don't use it. The field is additive
and optional to consumers: `evaluate_retrieval.py`'s own scoring never reads
it, so step 4's script and tests are unaffected.

Proposed commit breakdown:

1. **`docs: add Phase 2 step 5 plan`** (this section).
2. **`feat(evaluation): add ground-truth answers to the dataset`**
   - `evaluation/dataset.jsonl`: add `reference_answer` to all 25 lines.
   - `Question` (in `evaluate_retrieval.py`) gains a `reference_answer: str`
     field; `load_dataset()` parses it. Verified the same way step 4's
     `source_snippet`s were: every `reference_answer` is grounded in its
     paragraph's actual content, not fabricated beyond it.
3. **`build: add ragas as a dev dependency`**
   - `ragas` and the `langchain-community<0.4` pin (see above), both dev-only
     - never imported by `src/ragbridge`, matching decision 5's point that
     evaluation stays out of the shipped service.
   - **Bundled into this same commit, not a separate one:** replacing
     `httpx.Client` in `evaluate_retrieval.py`'s function signatures with a
     small `Protocol` (`post(...) -> a response with .raise_for_status() and
     .json()`). This dependency is *what breaks* `TestClient`'s typing (see
     above) - a commit that adds `ragas` alone would leave `mypy` failing
     until the next commit, and every commit here needs to pass all checks
     on its own, so the fix travels with the change that causes it.
4. **`feat(evaluation): add the RAGAS answer-quality evaluation script`**
   - New `evaluation/evaluate_answers.py`: `build_records(client, questions,
     *, top_k=5)` (posts each question to `/query`, assembles
     `{user_input, response, retrieved_contexts, reference}` records -
     RAGAS's field names) and `score_records(llm, embeddings, records)`
     (scores every record on all four metrics concurrently per record with
     `asyncio.gather` - safe here, unlike step 2's retrieval arms, because
     each metric call is an independent LLM request through its own client,
     not several queries sharing one constrained `AsyncSession`) and averages
     each metric with `statistics.mean`.
   - `main()` builds the judge LLM and embeddings as described above and
     prints all four scores - run by hand (decision 5).
   - New `tests/test_evaluate_answers.py`: the CI smoke test can only cover
     `build_records` (upload the corpus, call `/query`, assemble records) -
     it never calls `score_records`, because there is no fake judge LLM to
     call instead of a real one (unlike `Embedder`/`Chatter`/`Reranker`,
     nothing here is behind a `Protocol` with a `Fake` implementation: RAGAS's
     metrics are inherently "ask a real model," not a computation this
     codebase owns). The smoke test asserts each record has the four expected
     fields, correctly populated from the API response and the dataset.
5. **`docs(evaluation): add evaluation.md, README scores, and ADRs`**
   - `docs/evaluation.md`: how to run both evaluation scripts, and a results
     table stamped with the exact models and date used (decision 5) - **left
     as a template with no scores filled in**, not fabricated numbers: a real
     run needs the maintainer's own machine, and a score without a real run
     behind it is worse than no score. Early exploratory runs against local
     Ollama (`llama3.2`, done while verifying the API above) returned `0.0`
     for `faithfulness` and `context_recall` on an easy, obviously-supported
     example - a small 3B local model may not reliably produce the
     structured reasoning RAGAS's metrics ask for, which is itself worth
     recording as a real finding, not silently omitted.
   - README: short "Evaluation" section linking to `docs/evaluation.md`.
   - `docs/adr/0003-hybrid-search-with-reciprocal-rank-fusion.md`: why RRF
     over score-weighted blending (decision 1).
   - `docs/adr/0004-answer-quality-evaluation-with-ragas.md`: why RAGAS, why
     it runs outside CI (decision 5), and the two dependency issues found and
     fixed while building step 5, so a future upgrade of `ragas` or
     `langchain-community` has a documented reason not to just "clean up"
     the pin.
