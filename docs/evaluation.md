# Evaluation

`ragbridge` ships two evaluation scripts against a small, hand-written corpus and
question set in `evaluation/` - see `docs/plans/phase-2.md` for how the corpus and
dataset were built, and `docs/adr/0003-hybrid-search-with-reciprocal-rank-fusion.md`
/ `docs/adr/0004-answer-quality-evaluation-with-ragas.md` for why they work the way
they do.

Both scripts talk to a real, running `ragbridge` server over its public HTTP API -
they never call internal functions directly - so they measure exactly what a real
client experiences.

## Retrieval quality: recall@k and MRR

Needs only an embedder, not a judge model, so it is cheap enough to run often. CI
runs the same script against fakes as a smoke test (`tests/test_evaluate_retrieval.py`)
- that only proves the script still works, not that retrieval is any good; the
numbers below come from a real run.

```bash
docker compose up -d
uv run python -m evaluation.evaluate_retrieval --base-url http://localhost:8000
```

- **Recall@k**: fraction of the 25 questions where the right chunk appeared anywhere
  in the top `k` (default 5) returned sources.
- **MRR** (mean reciprocal rank): averages `1 / rank` of the right chunk when found,
  `0` when not - rewards it appearing *near the top*, not merely appearing.

## Answer quality: RAGAS

Needs a real judge LLM and embedder - there is no algorithmic stand-in for "is this
answer faithful to its context," so this always runs by hand, never in CI (see ADR
0004). CI's smoke test (`tests/test_evaluate_answers.py`) only proves the script
assembles records correctly; it never scores anything.

```bash
docker compose up -d
uv run python -m evaluation.evaluate_answers --base-url http://localhost:8000
```

By default the judge is `llama3.2` through Ollama's OpenAI-compatible endpoint - the
same local, key-free model the app itself uses for chat. Pass `--judge-model` to try
a different one (a larger local model, or a hosted one - point `OLLAMA_BASE_URL` at
an OpenAI-compatible endpoint, or adapt `main()` for a non-Ollama provider).

- **Faithfulness**: are the answer's claims supported by the retrieved context?
- **Answer relevancy**: does the answer actually address the question asked?
- **Context precision**: how much of the retrieved context is relevant, given the
  reference answer?
- **Context recall**: does the retrieved context contain what the reference answer
  needs?

A judge call can fail per metric per record (see ADR 0004's Consequences) - a
failure is excluded from that metric's average and reported separately, not counted
as a zero.

## Multi-step questions: `/agent` versus `/query`

The retrieval corpus above is six documents of under 1 KB each - about one chunk
apiece - so a single `/query` at `top_k=5` already returns 5 of the 6 chunks.
Retrieval there is capped near 100%, which leaves an agent nothing to improve, and
running `/agent` on it would only produce a meaningless tie. `evaluate_agent.py`
builds a corpus where the answer *cannot* be found from the question alone: chains
of four short documents (project -> owner -> manager -> email), with invented
names so no model can answer from memory, asked at three depths.

```bash
docker compose up -d
uv run ragbridge-admin create-tenant --name eval
uv run python -m evaluation.evaluate_agent --api-key <key>
```

- **1-hop** ("What is the email address of *Name*?") is the control: it names the
  person, so one search suffices. It checks that the agent does not do worse.
- **2-hop** names only the project, so the owner must be found first.
- **3-hop** names only the project and asks for the *manager's* email: three searches.
- **Answer document retrieved**: the document holding the answer is among the
  returned sources. **Answer correct**: the answer text contains the exact email
  address, so no LLM judge is needed (ADR 0004 found the default model unreliable as
  one). **Mean steps**: searches the agent ran (a `/query` is always one).

## Results

Every run below is stamped with the exact models and date used - a score without its
configuration is not comparable to anything.

### Retrieval (`evaluate_retrieval.py`)

| Date | Embedding model | Mode | Recall@5 | MRR |
|---|---|---|---|---|
| 2026-09-18 | `ollama/nomic-embed-text` | `hybrid` (default) | 100.0% | 0.960 |

All 25 questions retrieved their exact source chunk within the top 5 - unsurprising
for a corpus this small and this cleanly separated into distinct topics. MRR is
slightly below a perfect 1.0: two of the 25 ranked their correct chunk 2nd rather
than 1st (recomputed from the numbers: 23 questions at rank 1 plus 2 at rank 2 gives
exactly `(23 + 2 × 0.5) / 25 = 0.960`), still comfortably within `top_k`. Neither
number is evidence that hybrid search is unnecessary on this corpus: see
`tests/test_retrieval.py` and `tests/test_query.py` for cases, built deliberately
around a rare literal token (an error code), where `mode="vector"` alone does *not*
rank the right chunk first and hybrid mode does. A future, larger or messier corpus
is where this table's numbers would start to differentiate `hybrid` from
`vector`-only more visibly than a two-rank MRR gap.

### Answer quality (`evaluate_answers.py`)

| Date | Chat model | Judge model | Faithfulness | Answer relevancy | Context precision | Context recall |
|---|---|---|---|---|---|---|
| 2026-09-18 | `ollama/llama3.2` | `llama3.2` (via Ollama) | 1.000 (n=1/25) | 0.734 (n=20/25) | 0.922 (n=20/25) | 0.928 (n=25/25) |

**Read the sample sizes before the scores.** `llama3.2` (3B, local, CPU) failed
`faithfulness`'s structured-output task on **24 of 25 records** - it repeatedly
echoed the JSON *schema* it was given back as if it were the filled-in answer,
which fails `instructor`'s validation after every retry (see ADR 0004). The `1.000`
faithfulness score is real, but it is an average over a single successful record,
not a trustworthy measure of the app's actual faithfulness - treat it as "this
judge model cannot really run this metric," not as "faithfulness is perfect."
`answer_relevancy` and `context_precision` failed on 5 of 25 each (still worth a
grain of salt); `context_recall` completed on all 25 and is the most trustworthy
number in this row. **The practical conclusion, not a hedge:** `llama3.2` is not a
reliable RAGAS judge for `faithfulness` specifically. Re-run with a larger local
model (`--judge-model llama3.1:8b` or similar, if pulled) or a hosted one (point
`AsyncOpenAI` in `evaluate_answers.py` at a real provider) before trusting that
column. Run `evaluate_answers.py` yourself to see the exact per-record validation
errors on stderr (the same `instructor.v2.core.errors.InstructorRetryException`
detail shown while building this script - see ADR 0004).

### Agent versus query (`evaluate_agent.py`)

2026-09-19. `ollama/llama3.2` answers **and** plans (the defaults),
`ollama/nomic-embed-text` embeds, hybrid retrieval, no reranking,
`AGENT_MAX_STEPS=3`. 60 documents (15 chains), 45 questions, each asked twice.

| Endpoint | Tier | Questions | Answer document retrieved | Answer correct | Mean steps |
|---|---|---|---|---|---|
| `/query` | 1-hop | 30 | 100% (30/30) | 100% (30/30) | - |
| `/query` | 2-hop | 30 | 7% (2/30) | 7% (2/30) | - |
| `/query` | 3-hop | 30 | 13% (4/30) | 0% (0/30) | - |
| `/agent` | 1-hop | 30 | 100% (30/30) | 100% (30/30) | 1.00 |
| `/agent` | 2-hop | 30 | 13% (4/30) | 17% (5/30) | 1.13 |
| `/agent` | 3-hop | 30 | 13% (4/30) | 7% (2/30) | 1.50 |

**The honest reading: with the default model, `/agent` is not meaningfully better
than `/query` on multi-hop questions.** The 1-hop control is 100% for both, so
nothing regressed and the harness works. On the multi-hop tiers `/agent` answered 5
and 2 of 30 correctly against `/query`'s 2 and 0. The two runs repeat the same 15
questions, so the effective sample is 15, and differences that small are within what
model nondeterminism produces. **No benefit can be claimed from these numbers.**

The step counts say why. One `/agent` run per multi-hop question, inspected by hand
(15 questions per tier), shows three separate behaviours:

1. **Most runs stop after one search** - 12 of 15 at 2-hop and 9 of 15 at 3-hop. The
   planner reads five chunks that do not contain the answer and still replies
   "answer now". This is the dominant cause.
2. **When it does search again it sometimes reasons correctly.** For one 3-hop
   question it took the owner's name out of the first result and searched
   `manager of <that name>` - genuine multi-hop behaviour - though it then stopped
   one hop short. Another 3-hop run retrieved the answer document and answered
   correctly.
3. **Sometimes it paraphrases the question instead of using the finding**
   (`email address of owner of Project ...`), which finds nothing new.

So the loop itself works - a scripted planner is exercised in
`tests/test_agent_loop.py`, and real runs above reached the right document - but
`llama3.2` is an unreliable planner. **Not measured:** any stronger planner model
(no hosted-provider key was available for this run), and any tuning of the planner
prompt, which is the first lever to try. Both are set through `AGENT_PLANNER_MODEL`
and `PLANNER_PROMPT` in `ragbridge.agent.planner`; re-run this script after either.

