# 6. A hand-rolled agent loop instead of LangGraph

Date: 2026-09-18

## Status

Accepted. Supersedes the roadmap's original "agent mode with LangGraph".

## Context

Phase 4 adds `POST /agent`: a question that may need more than one search, such as
"how does our refund policy differ from our cancellation policy?", whose answer
lives in two places one embedding cannot point at at once. The original roadmap
named LangGraph for this.

`langgraph` hard-requires `langchain-core<2,>=1.4.7`. Adopting it would put the
LangChain ecosystem into this project's **runtime** dependencies, and Phase 1
(decision 3) already rejected LangChain once - the project wrote its own chunker
instead. Meanwhile everything the loop needs already exists as plain async
functions: retrieval, fusion, reranking, generation. Before deciding, the loop was
prototyped against the real library (`langgraph==1.2.11`): it works, and it comes to
about twenty lines of graph wiring (a `StateGraph`, one conditional edge, a step
counter) around the same function calls a `while` loop makes.

`langchain-core` is already installed here, but **only as a dev dependency** of
`ragas` (Phase 2). Keeping it dev-only is the point of this decision.

A second constraint shapes the design more than the framework question does.
`docs/adr/0004-...` measured the default `CHAT_MODEL` (`llama3.2`) failing RAGAS's
structured-output contract on **24 of 25 records**, echoing the JSON schema back
instead of filling it in. An agent whose control flow depends on structured output
from that model must not assume its output parses. (That measurement was of RAGAS's
schema, a stricter contract than the planner's two-field object; see the measurement
under Consequences.)

## Decision

Write the loop by hand, in `ragbridge.agent.loop.run_agent`, behind a `Planner`
`Protocol` in `ragbridge.agent.planner`.

- **`Planner` is its own protocol**, not a new method on `Chatter`. It is the sixth
  provider seam after `Embedder`, `Chatter`, `Reranker`, `JobQueue` and `Cache`,
  for the same reason as the others: a real implementation (`LiteLLMPlanner`) and a
  scripted one (`FakePlanner`) that lets CI exercise a *multi-step* run
  deterministically without a network call.
- **A decision that cannot be parsed means "answer now"** - never an exception and
  never a retry. Invalid JSON, a non-object, a missing or unknown `action`, and a
  `search` with no query all resolve to answering with what has been found, and each
  is logged.
- **The first search always uses the question itself.** The planner is only asked
  what to do *after* a search. Together with the previous rule, a model that only
  ever fails to produce a decision yields exactly one search: today's single-shot
  `POST /query` behaviour. A weak model degrades to that; a strong model gets
  multi-step retrieval; nothing in between breaks.
- **The step ceiling is enforced in code.** `AGENT_MAX_STEPS` (default 3) bounds the
  loop; a request may ask for fewer steps, never more, and the planner is not even
  consulted once the budget is spent. An agent that sets its own budget is a cost
  incident waiting for a prompt injection - "you may search at most three times" in
  a prompt is a suggestion, while `len(steps) >= max_steps` is a guarantee.
- **Chunks found by several searches are kept once, with their best score**, so
  overlapping searches do not spend the context budget on repeats.
- **`AGENT_PLANNER_MODEL`** (empty by default, meaning reuse `CHAT_MODEL`) lets an
  installation point planning - which needs reliable JSON - at a stronger model than
  answering, which needs good prose.

## Consequences

- **Given up, stated honestly:** checkpointing (resuming a partial run), built-in
  streaming primitives, and a vocabulary other developers already know. If one of
  these becomes a real requirement rather than a hypothetical one, the loop lives
  behind a single function and can be swapped.
- **No new runtime dependency.** The loop is about 30 lines with no database
  dependency - retrieval is passed in as a callable - so it is tested with
  `FakePlanner` and a fake search, and never needs a model or a session.
- **Measured with the default model, after building it:** `llama3.2` was called as the
  planner on 12 prompts (6 questions, each twice, against a real Ollama). All 12 replies
  parsed - **no fallback fired** - so the strict fallback rule above is a safety net,
  not the common path. It made sensible decisions (searching again for the second half
  of a comparison, answering simple questions directly), but not consistently: the same
  prompt gave a different decision on a second run, and one search repeated the
  question almost verbatim (`support hours`), which wastes a step - harmlessly, since
  results are de-duplicated by chunk id. This was a 12-call sample of *whether the JSON
  parses and what it decides*, not of answer quality.
- **Answer quality, measured in Phase 5** (`docs/evaluation.md`): on a synthetic corpus
  where the answer is reachable only through a chain of documents, `/agent` with the
  default `llama3.2` planner answered 5 and 2 of 30 questions correctly at two and
  three hops, against `/query`'s 2 and 0 - **within noise, so no benefit can be
  claimed.** The cause is the planner: on inspection, most runs (12 of 15 at two hops,
  9 of 15 at three) stopped after one search, because it judged the first results
  sufficient. When it did search again it sometimes used a name from the findings
  correctly, and one three-hop run reached the right document and answered correctly.
  So the loop works and the default planner is the weak part.
- **Then measured with a stronger planner** (`docs/evaluation.md`): `qwen2.5:7b` answered 29 of
  30 two-hop questions correctly in two separate runs, against 2-3 for `llama3.2` and
  `llama3.1:8b`, confirming the planner - not the loop - was the limit. Three hops stay
  unreliable (13-20% at the default limit of 3 searches, 33% at 5). Size alone did not
  explain it: `llama3.1:8b` was no better than the 3B model. Not measured: prompt tuning,
  and models beyond these three.
- Each `/agent` call is independent: there are no sessions or follow-up questions
  (see `docs/plans/phase-4.md`, decision 7).
