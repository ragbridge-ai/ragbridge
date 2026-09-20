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

## Choosing a chat model

Two more scripts compare chat models, and they measure different things - a model can
be good and too slow, or fast and unreliable.

- **`evaluate_qa.py` - answer quality, with no LLM judge.** RAGAS needs a judge model and
  ADR 0004 found the default one unreliable, so this scores answers *mechanically*, with
  regular expressions. 43 questions over the Phase 2 corpus: the 25 Phase 2 lookups; 10
  *paraphrased* questions that share almost no keywords with the text ("how often do I have
  to replace my password?" for "changed every 180 days"); and 8 questions the documents do
  not answer, where saying so is correct and inventing an answer is the failure. Its tests
  prove the scorer is trustworthy: every check accepts its own good example, an evasive
  answer never passes an answerable question, and one check that wrongly accepted "I don't
  know" was caught and fixed while writing them.
- **`measure_model_speed.py` - speed and memory, from Ollama's own counters.** No ragbridge
  involved: tokens per second writing and reading, cold load time, and how much of the model
  sits on the GPU. Run it on the machine you will deploy to.

```bash
uv run python -m evaluation.evaluate_qa --api-key <key> --label ollama/qwen2.5:7b --show-failures
uv run python -m evaluation.measure_model_speed llama3.2 qwen2.5:7b
```

The chat model is set on the server (`CHAT_MODEL`); the embedding model does not change
between comparisons, so uploaded documents stay valid.

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

**The honest reading: with the default model (`llama3.2`), `/agent` is not meaningfully
better than `/query` on multi-hop questions - but that is a fact about that model, not about
the loop: see [Chat models compared](#chat-models-compared), where a 7B model makes 2-hop
questions work.** The 1-hop control is 100% for both, so
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

### Ranking on a larger corpus (`evaluate_ranking.py`)

The Acme corpus above is too small for a ranking change to show: about 30 chunks, and one
search returns most of them. `evaluate_ranking.py` uses a separate synthetic corpus of **100
chunks**: 91 generic paragraphs that keep repeating a fictional product name, 4 distractors that
use the same words without answering, 4 fact chunks, and the technology table of `AGENTS.md`
copied verbatim (19 rows) as one long table chunk. Its 16 questions are short and long, with and
without the product name, one fact or two, plus four controls. It needs a live server with a real
embedder; run it against a scratch tenant:

```bash
uv run python -m evaluation.evaluate_ranking --api-key <key> --agent
```

`recall@k` is the share of the expected chunks in the first k results, `complete@5` is 1 only
when all of them are, and `MRR` is 1 / the rank of the first one. Measured with
`nomic-embed-text` (the agent used `qwen2.5:7b`), 2026-09-20:

| Mode | Change | recall@1 | recall@5 | recall@10 | complete@5 | MRR |
|---|---|---|---|---|---|---|
| vector | none | 0.22 | 0.31 | 0.31 | 0.25 | 0.33 |
| keyword | before | 0.41 | 0.75 | 0.75 | 0.75 | 0.58 |
| keyword | words in most chunks left out | 0.78 | **1.00** | 1.00 | 1.00 | 0.94 |
| hybrid (default) | before | 0.16 | 0.44 | 0.81 | 0.38 | 0.39 |
| hybrid (default) | words in most chunks left out | 0.22 | **0.97** | **1.00** | **0.94** | 0.60 |
| agent | before | 0.12 | 0.44 | 0.44 | 0.38 | 0.32 |
| agent | words in most chunks left out | 0.22 | 0.97 | 1.00 | 0.94 | 0.60 |

What the baseline showed: the right chunk was usually *found* but at rank 7 to 9, behind generic
chunks. The product name was not the only cause: the vector arm found the long table chunk in the
top 5 for **0 of 4** one-fact questions even without the name, so a long table embeds badly. The
keyword arm without the name was excellent and collapsed with it, because a strict AND on the
name matched only distractors, and under the OR fallback every generic chunk that shared the name
took keyword credit. Leaving out words that occur in most chunks fixed that. The agent did not
help at baseline (the same recall@5, 2.19 searches per question) and, once the first search
works, uses 1.12.

Limits, stated plainly: 16 questions on one synthetic corpus; recall@1 stays low (0.22) because
the distractors deliberately contain the question's own words; the vector arm is unchanged and
still weak on the long table chunk; one two-chunk question (web framework plus license) still
ranks the table 6th.

#### Rarity weighting of the keyword fallback (held-out questions)

`ts_rank` weighs every word the same, so in the OR fallback a chunk matching three common words
outranks the one chunk with the single rare word. `KEYWORD_RARITY_WEIGHTING` ranks by the summed
`ln(chunks / chunks containing the word)` instead. Because the 16 questions above were used to
choose the previous change, two **new** sets were written and committed before this was
implemented, and were run once before and once after, without tuning:

| Held-out set | Mode | recall@5 | recall@1 | MRR |
|---|---|---|---|---|
| synthetic corpus, 12 questions | keyword | 1.00 -> 1.00 | 0.79 -> 0.79 | 0.96 -> 0.96 |
| synthetic corpus, 12 questions | hybrid | 0.96 -> 0.96 | 0.50 -> 0.50 | 0.72 -> 0.72 |
| real prose (`AGENTS.md`, `phase-4.md`, `phase-5.md`), 8 questions | keyword | 0.88 -> 0.88 | 0.75 -> 0.75 | 0.79 -> **0.81** |
| real prose, 8 questions | hybrid | 0.88 -> 0.88 | 0.62 -> 0.62 | 0.73 -> **0.75** |
| the original 16 (not held out; regression check) | hybrid | 0.97 -> 0.97 | 0.22 -> 0.22 | 0.60 -> 0.60 |
| Acme (`evaluate_retrieval`) | hybrid | 100% -> 100% | | 0.940 -> **0.960** |

The effect is **small**: most of these questions match strictly and never reach the fallback,
and the synthetic set was already near its ceiling. Nothing regressed. The real-prose set is
reproducible on the public files:

```bash
uv run python -m evaluation.evaluate_ranking --api-key <scratch-tenant-key> \
  --dataset evaluation/docs_heldout.jsonl \
  --files AGENTS.md docs/plans/phase-4.md docs/plans/phase-5.md
```

**What it does not fix, measured.** A chunk that contains a rare word can be the keyword arm's
#1 and still rank 10th to 15th in the hybrid result. On a real question about "Swagger", the one
chunk containing that word was **keyword #1 before and after** (so the OR ranking was never its
problem) and **absent from the vector arm's top 20**. Its fused score is then `1/61 = 0.0164`,
the most any single-arm chunk can get, and eleven chunks beat it by being present in *both* lists
at middling ranks (a chunk at vector #19 and keyword #18 scores 0.0255). Reciprocal rank fusion
favours weak agreement between two arms over one arm's certainty, and the vector arm does not
retrieve the chunk at all. Rarity weighting cannot change that: it only reorders inside the
keyword arm. The next options are in the pull request that added this section.

#### Does one rare word pull chunks from unrelated documents? (measured, nothing kept)

After rarity weighting, a reported answer had unrelated chunks from other documents in 2 of its
5 scored slots. To see whether the keyword arm is the cause, a third held-out set was written
before any variant was tried: 18 questions on a mixed corpus of one invented CV and three
technical documents that share words with it ("jobs", "Docker", "container"; 26 chunks,
`evaluation/mixed/`). Three guards were prototyped, run on identical data (a scratch tenant per
corpus, `nomic-embed-text`, no chat model), and reverted:

- **B, require 2 matched terms** in the OR fallback (or 1, when only one word occurs anywhere);
- **C, prefer 2-term chunks**: rank chunks matching two or more words before the others;
- **D, cap one word's weight** at half of `ln(chunks)`, so one word cannot outweigh two rare ones.

Hybrid mode, recall@5 / MRR. "Foreign" is the mean number of the top 5 results that come from a
document that does not hold the answer (mixed and real-prose sets).

| Variant | synthetic (12) | real prose (8) | mixed (18) | original 16 | Acme (25) | foreign, mixed: keyword / hybrid | foreign, real prose: keyword / hybrid |
|---|---|---|---|---|---|---|---|
| current (rarity weighting on) | 0.96 / 0.725 | 0.88 / 0.750 | 1.00 / 0.917 | 0.97 / 0.596 | 1.00 / 0.960 | 2.00 / 2.39 | 1.62 / 1.62 |
| rarity weighting off (reference) | 0.96 / 0.725 | 0.88 / 0.750 | 1.00 / 0.852 | 0.97 / 0.596 | 1.00 / 0.960 | | |
| B require 2 terms | 0.92 / 0.736 | 0.88 / 0.819 | 1.00 / 0.917 | 1.00 / 0.604 | 1.00 / 0.960 | **0.61** / 2.11 | 0.62 / 1.75 |
| C prefer 2-term chunks | 0.96 / 0.725 | 0.88 / 0.750 | 1.00 / 0.917 | 0.97 / 0.596 | 1.00 / 0.960 | 2.00 / 2.44 | 1.50 / 1.75 |
| D cap one word | 0.96 / 0.725 | 0.88 / 0.750 | 1.00 / 0.852 | 0.97 / 0.596 | 1.00 / 0.940 | 2.00 / 2.39 | 1.50 / 1.75 |

- The pull is real **in the keyword arm**: requiring two terms cuts foreign chunks in its top 5
  from 2.00 to 0.61 (mixed) and from 1.62 to 0.62 (real prose).
- It **does not reach the hybrid result**: foreign chunks there barely move (2.39 to 2.11), because
  the vector arm supplies them on its own (a Docker question is close to every Docker chunk).
- Requiring two terms costs keyword-mode recall@5 where the answer has one informative word:
  1.00 to 0.88 on the synthetic set (2 questions) and 1.00 to 0.94 on the mixed set (the "Python
  3.12" table chunk). In hybrid it is a wash: it loses one synthetic question and recovers two
  others.
- Preferring 2-term chunks changes no recall or MRR. Capping a word's weight makes the mixed set
  (MRR 0.917 to 0.852) and Acme (0.960 to 0.940) worse.

No guard beats the current behaviour in hybrid mode, so none was kept. What fills the scored slots
with foreign chunks is the vector arm and the fusion, which is what a reranker addresses.
Reproduce with `--dataset evaluation/mixed_heldout.jsonl --files evaluation/mixed/*.md`.

### Chat models compared (`evaluate_qa.py`, `measure_model_speed.py`, `evaluate_agent.py`)

2026-09-19, on an **Apple M1 Pro with 16 GB** (macOS 27.0, Ollama 0.34.1, Docker Desktop's VM
capped at 3.83 GiB). These are real-world conditions, not a clean lab: Chrome, Cursor and
this assistant were running, and the Mac already had 5.7-8.7 GB of swap in use throughout
(Chrome alone held about 4 GB). Embeddings: `nomic-embed-text` in every run. The candidates
were chosen from the Ollama library's current sizes as the largest that fit alongside
everything else in 16 GB.

**Speed and memory** (Ollama's counters; "one RAG call" reads about 1,150 tokens of context,
like five retrieved chunks):

| Model | Writes (tokens/s) | Reads a RAG prompt (tokens/s) | One RAG call | Cold load | Memory when loaded | On the GPU |
|---|---|---|---|---|---|---|
| `llama3.2` (3B) | 54 | 522 | 2.6 s | 1.9 s | 2.55 GB | 100% |
| `qwen2.5:7b` | 26 | 230 | 6.0 s | 3.1 s | 4.74 GB | 100% |
| `llama3.1:8b` | 24 | 215 | 5.8 s | 3.4 s | 5.26 GB | 100% |

All three ran entirely on the GPU with steady speed across repeats (`qwen2.5:7b`: 25.6, 25.5,
25.7 tokens/s), so there was no throttling. The Mac swapped 1.2 GB more during the small
model's run and 1.5-1.8 GB more during the 7-8B ones, so the bigger models cost only
0.3-0.6 GB more swap than the small one - the pressure comes from the other applications.

**Answer quality**, 43 questions, four runs each (one initial run and three repeats, because
a single run of a model that samples its answers is not trustworthy):

| Model | Score per run (of 43) | Average | Median seconds per question |
|---|---|---|---|
| `llama3.2` (3B) | 39, 40, 38, 40 | 39.2 (91%) | 0.7 |
| `qwen2.5:7b` | 43, 43, 43, 43 | **43.0 (100%)** | 1.7 |
| `llama3.1:8b` | 41, 41, 40, 39 | 40.2 (94%) | 1.4 |

`qwen2.5:7b` passed every question in every run. `llama3.1:8b`'s and `llama3.2`'s ranges
(39-41 and 38-40) overlap, so **the larger `llama3.1:8b` is not reliably better at answering
than the 3B model** - size alone did not help. Failures were mostly a wrongly evasive "I don't
know" on an answerable question, and an answer that gave "12 characters" but left out the
"180 days" rotation (two models). One check (a leading "Yes" to "can I still get a refund
after 3 weeks?") is a heuristic and may misjudge a hedged answer.

**Multi-step questions with `/agent`**, correct out of 30 (15 distinct questions, each asked
twice, so the effective sample is smaller than 30):

| Model | 2 hops, run 1 | 2 hops, rerun | 3 hops, run 1 | 3 hops, rerun | Mean searches (2 hops) |
|---|---|---|---|---|---|
| `llama3.2` (3B) | 3 | - | 7 | - | 1.10 |
| `qwen2.5:7b` | **29** | **29** | 6 | 4 | 1.97 / 2.00 |
| `qwen2.5:7b`, `AGENT_MAX_STEPS=5` | 29 | - | **10** | - | 1.93 (3.33 at 3 hops) |
| `llama3.1:8b` | 2 | - | 3 | - | 1.00 |
| plain `/query`, any model | 1-2 | - | 0 | - | 1 |

**`qwen2.5:7b` makes `/agent` work at two hops** - 29 of 30 twice, with almost exactly the two
searches the question needs - where `llama3.2` and `llama3.1:8b` mostly stop after one search.
This overturns the conclusion above *for this model*: the loop was never the weak part, the
planner was. **Three hops remain unreliable**: 13-20% correct at the default limit of 3
searches, and 33% with a limit of 5 (3.3 searches on average). A 3-hop question needs exactly
three searches, so the default limit leaves no slack; the improvement from raising it is
suggestive, not conclusive, given the sample, and each extra search is another model call.

**What to use on a 16 GB laptop:** `qwen2.5:7b` (`CHAT_MODEL=ollama/qwen2.5:7b`, and
`AGENT_PLANNER_MODEL` if you set one). It is about half the speed of `llama3.2` and still fast
(about 1.7 s per question end to end). `llama3.2` stays a reasonable choice when speed or
memory matter more than accuracy. **Not measured:** models other than these three, other
languages, larger documents, and the same models on a server CPU - this was a GPU laptop.
`qwen2.5:7b` loads at 4.7 GB, so it does not fit alongside the production stack on a 4 GB
server (see [deployment.md](deployment.md)).

