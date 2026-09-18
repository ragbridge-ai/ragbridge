# 4. Answer-quality evaluation with RAGAS

Date: 2026-09-18

## Status

Accepted

## Context

`evaluation/evaluate_retrieval.py` (Phase 2 step 4) measures whether retrieval finds
the right chunk - recall@k and MRR - using only an embedder, no LLM judge, so it is
cheap enough to run in CI against fakes as a smoke test. It cannot measure the thing
retrieval can't see: whether the *generated answer* is actually faithful to the
retrieved context and relevant to the question. That needs a model to judge, not an
algorithm, which is a different cost and reliability profile from step 4's script -
covered here as its own decision, matching the roadmap in `AGENTS.md`
("RAGAS evaluation with scores in the README").

RAGAS is the most established open-source library for exactly this: faithfulness,
answer relevancy, context precision, and context recall, computed by prompting a
judge LLM with structured-output requests. Two real issues surfaced while wiring it
up, both verified against the actually-installed version (`ragas==0.4.3`) and a real
local Ollama instance before being worked around - this project's RAGAS integration
has moved fast enough across versions that guessing from documentation or training
data would not have produced working code.

### Issue 1: `ragas` fails to import against the current `langchain-community`

`ragas==0.4.3` unconditionally imports `langchain_community.chat_models.vertexai` at
`import ragas` time. `langchain-community` is being sunset in favour of standalone
per-provider packages, and removed that submodule starting in `0.4`. Installing
`ragas` alongside the latest `langchain-community` therefore fails immediately with
`ModuleNotFoundError`, before any of this project's own code runs.

### Issue 2: installing `ragas` changes `TestClient`'s base class

`ragas` pulls in `langchain-core`, which depends on `langsmith`, which depends on a
package called `httpx2`. Once `httpx2` is importable, `starlette.testclient.TestClient`
switches its base class from `httpx.Client` to `httpx2.Client` (`starlette` had been
warning about this migration in every test run since Phase 1: "Using httpx with
starlette.testclient is deprecated; install httpx2 instead"). This broke
`evaluate_retrieval.py`'s `client: httpx.Client` type hints from step 4 - `TestClient`
is no longer nominally an `httpx.Client`, though it remained behaviourally identical
(only `mypy` caught it; the existing smoke test kept passing at runtime).

### Issue 3: `ragas`'s modern metrics need an instructor-based LLM, and `evaluate()` rejects them

`ragas.metrics.collections.{Faithfulness,AnswerRelevancy,ContextPrecision,
ContextRecall}` - the recommended, non-deprecated metric classes - require a "modern
instructor-based" LLM (`ragas.llms.llm_factory`, wrapping an `AsyncOpenAI`-shaped
client) and explicitly reject the classic `LangchainLLMWrapper` path with a clear
error. Separately, the top-level `ragas.evaluate()` batch orchestrator does not yet
accept these newer metric objects at all (`TypeError: All metrics must be initialised
metric objects`), even though it is the API RAGAS's own documentation leads with.

## Decision

- Pin `langchain-community<0.4` alongside `ragas` in the dev dependency group (issue
  1), with a comment in `pyproject.toml` explaining why, so a future "clean up this
  pin" pass has a documented reason not to.
- Replace `evaluate_retrieval.py`'s `httpx.Client` type hints with a small structural
  `HttpClient` Protocol (issue 2) - the same "depend on the shape, not a concrete
  class" idiom already used for `Embedder`, `Chatter`, and `Reranker` - rather than
  fighting which HTTP library `TestClient` happens to subclass.
- `evaluation/evaluate_answers.py` builds the judge LLM via `llm_factory`, pointed at
  Ollama's OpenAI-compatible endpoint (`AsyncOpenAI(base_url=f"{OLLAMA_BASE_URL}/v1",
  api_key="ollama")`) by default (issue 3) - the same local, key-free setup the rest
  of the app already assumes, no new provider required to try it. Embeddings use
  `ragas.embeddings.LiteLLMEmbeddings`, matching `EMBEDDING_MODEL`. Each metric's
  `.ascore(...)` is called directly (concurrently per record, with `asyncio.gather` -
  safe here because each call is an independent LLM request, not several queries
  sharing one constrained `AsyncSession` the way `hybrid_search`'s arms do) instead
  of going through `ragas.evaluate()`.
- Evaluation runs by hand, never in CI, matching the recall@k/MRR decision - but for
  a stronger reason here: there is no algorithmic stand-in for a judge LLM the way
  `FakeEmbedder` stands in for a real one. CI's smoke test
  (`tests/test_evaluate_answers.py`) covers only `build_records` - upload the corpus,
  call `POST /query`, assemble RAGAS-shaped records - and never calls `score_records`.
- Each metric call can fail independently per record without crashing the whole run.
  This was not a defensive design choice made in advance - it was forced by a real
  failure during verification (see Consequences) and implemented in response to it.

## Consequences

- Running the full pipeline for real, against local Ollama with `llama3.2` as the
  judge, surfaced a real reliability limit worth recording rather than hiding: a
  small (3B) local model does not reliably follow RAGAS's structured-output
  contract. The full 25-question run (`docs/evaluation.md` has the complete table)
  found `faithfulness` failing on **24 of 25 records** - the model repeatedly
  echoed the JSON *schema* it was given back as if it were the filled-in answer,
  which fails `instructor`'s validation after every retry - while
  `answer_relevancy` and `context_precision` failed on 5 of 25 each, and
  `context_recall` completed cleanly on all 25. The reported `faithfulness: 1.000`
  is real but is an average over a single successful record, not a trustworthy
  signal; `docs/evaluation.md` states this plainly rather than presenting the
  number at face value. `evaluate_answers.py` catches this per metric per record
  (returns `None`, prints a warning, excludes it from that metric's average)
  rather than letting one bad record crash the whole evaluation - and raises only
  if every single record fails a given metric, since an average over zero
  successes would be meaningless. `--judge-model` lets the maintainer try a
  stronger local or hosted model if failure counts are high; see
  `docs/evaluation.md` for the actual run's numbers and failure counts.
- `pyproject.toml`'s dev group gained a genuinely heavy transitive dependency tree
  (`langchain`, `pandas`, `numpy`, `pyarrow`, ...) - acceptable because it is
  dev-only and never imported by `src/ragbridge`, but real: `uv sync` for
  development is now noticeably larger than it was before this step.
- The `langchain-community<0.4` pin and the `HttpClient` Protocol are both
  workarounds for specific, verified versions of `ragas` and its dependency tree.
  An upgrade of `ragas` should re-verify both - the pin might no longer be needed,
  or the Protocol might need to change shape again if `ragas`'s own dependencies
  shift which HTTP library `TestClient` resolves to.
