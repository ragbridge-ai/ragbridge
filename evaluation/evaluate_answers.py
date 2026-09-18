"""Evaluate answer quality against the evaluation/ corpus and dataset, using RAGAS.

Unlike evaluate_retrieval.py, this cannot be faked (Phase 1 decision 5):
there is no algorithmic way to compute "is this answer faithful to its
context" without asking a real model, so this always calls a real judge
LLM and embedder, and is always run by hand (docs/plans/phase-2.md, step
5), never in CI - see tests/test_evaluate_answers.py for what the CI
smoke test covers instead.

Four metrics, from ragas.metrics.collections:
- faithfulness: are the answer's claims supported by the retrieved context?
- answer_relevancy: does the answer actually address the question?
- context_precision: how much of the retrieved context is relevant?
- context_recall: does the retrieved context contain what the reference
  answer needs?

Verified while building this script, against ragas 0.4.3 and a real local
Ollama instance (not assumed from documentation - RAGAS's API has moved
enough across versions that guessing would not have worked):

- The newer, non-deprecated ragas.metrics.collections classes require a
  "modern instructor-based" LLM (ragas.llms.llm_factory, wrapping an
  AsyncOpenAI-shaped client) - they reject the classic LangchainLLMWrapper
  path outright. Ollama serves an OpenAI-compatible API at /v1, so
  llm_factory is pointed there by default: the same local, key-free judge
  setup the rest of the app already assumes, no new provider required.
- ragas.evaluate()'s batch orchestrator does not accept these newer
  metric objects ("All metrics must be initialised metric objects"), so
  this script calls each metric's .ascore(...) directly instead.

Run for real, against a running server with a real embedder configured
(docker compose up first):

    uv run python -m evaluation.evaluate_answers --base-url http://localhost:8000
"""

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
from ragas.embeddings import LiteLLMEmbeddings
from ragas.llms import InstructorBaseRagasLLM, llm_factory
from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

from evaluation.evaluate_retrieval import HttpClient, Question, load_dataset, upload_corpus
from ragbridge.config import get_settings


@dataclass(frozen=True)
class AnswerQualityResult:
    questions_evaluated: int
    faithfulness: float
    """Fraction of the answer's claims that the retrieved context supports."""
    answer_relevancy: float
    """How directly the answer addresses the question (embedding similarity
    between the question and questions reconstructed from the answer)."""
    context_precision: float
    """How much of the retrieved context is actually relevant, given the
    reference answer."""
    context_recall: float
    """How much of what the reference answer needs is present in the
    retrieved context."""
    failures: dict[str, int]
    """How many records failed to score, per metric - excluded from that
    metric's average, not silently ignored. A real possibility with a
    small local judge model, not a theoretical one: verified while
    building this script, llama3.2 sometimes echoes the JSON schema back
    instead of filling it in, failing structured-output validation after
    every retry (see docs/evaluation.md). A stronger --judge-model is the
    fix, not a code change here.
    """


@dataclass(frozen=True)
class _Metrics:
    """The four constructed metric objects, built once and reused per record."""

    faithfulness: Faithfulness
    answer_relevancy: AnswerRelevancy
    context_precision: ContextPrecision
    context_recall: ContextRecall


def build_records(
    client: HttpClient, questions: list[Question], *, top_k: int = 5
) -> list[dict[str, Any]]:
    """Run every question through POST /query and assemble RAGAS-shaped records.

    Each record has the four fields ragas.metrics.collections needs:
    user_input (the question), response (the generated answer),
    retrieved_contexts (the returned sources' snippets), and reference
    (the dataset's reference_answer - needed by context_precision and
    context_recall, see docs/plans/phase-2.md, step 5).
    """
    records = []
    for question in questions:
        response = client.post("/query", json={"question": question.question, "top_k": top_k})
        response.raise_for_status()
        body = response.json()
        records.append(
            {
                "user_input": question.question,
                "response": body["answer"],
                "retrieved_contexts": [source["snippet"] for source in body["sources"]],
                "reference": question.reference_answer,
            }
        )
    return records


async def _score_one(coro: Any, label: str) -> float | None:
    """Run one metric's ascore(...) call, returning None instead of raising on failure.

    A judge LLM call can fail unpredictably per record and per metric -
    not a hypothetical: verified while building this script, a small
    local model (llama3.2) sometimes echoes the JSON schema it was given
    back as if it were the answer, which fails instructor's
    structured-output validation after every retry and raises. One bad
    record must not crash the whole evaluation run, so the exception is
    caught broadly here on purpose - any failure from an external judge
    call becomes a recorded miss, not a crash.
    """
    try:
        result = await coro
        return float(result.value)
    except Exception as error:  # broad on purpose: any judge-call failure must not crash the run
        print(f"  ! {label} failed on one record ({type(error).__name__})", file=sys.stderr)
        return None


async def _score_record(metrics: _Metrics, record: dict[str, Any]) -> dict[str, float | None]:
    """Score one record on all four metrics.

    Runs the four metric calls concurrently with asyncio.gather - safe
    here, unlike hybrid_search's retrieval arms (Phase 2 step 2): each
    metric call is an independent request through its own LLM/embeddings
    client, not several queries sharing one constrained AsyncSession.
    """
    faithfulness, answer_relevancy, context_precision, context_recall = await asyncio.gather(
        _score_one(
            metrics.faithfulness.ascore(
                user_input=record["user_input"],
                response=record["response"],
                retrieved_contexts=record["retrieved_contexts"],
            ),
            "faithfulness",
        ),
        _score_one(
            metrics.answer_relevancy.ascore(
                user_input=record["user_input"], response=record["response"]
            ),
            "answer_relevancy",
        ),
        _score_one(
            metrics.context_precision.ascore(
                user_input=record["user_input"],
                retrieved_contexts=record["retrieved_contexts"],
                reference=record["reference"],
            ),
            "context_precision",
        ),
        _score_one(
            metrics.context_recall.ascore(
                user_input=record["user_input"],
                retrieved_contexts=record["retrieved_contexts"],
                reference=record["reference"],
            ),
            "context_recall",
        ),
    )
    return {
        "faithfulness": faithfulness,
        "answer_relevancy": answer_relevancy,
        "context_precision": context_precision,
        "context_recall": context_recall,
    }


async def score_records(
    llm: InstructorBaseRagasLLM, embeddings: LiteLLMEmbeddings, records: list[dict[str, Any]]
) -> AnswerQualityResult:
    """Score every record on all four RAGAS metrics and average each.

    Raises RuntimeError if a metric fails on every single record - at
    that point the judge model isn't usable for this metric at all, and
    an average over zero successes would be meaningless, not just noisy.
    """
    metrics = _Metrics(
        faithfulness=Faithfulness(llm=llm),
        answer_relevancy=AnswerRelevancy(llm=llm, embeddings=embeddings),
        context_precision=ContextPrecision(llm=llm),
        context_recall=ContextRecall(llm=llm),
    )

    scored = [await _score_record(metrics, record) for record in records]

    def average(name: str) -> float:
        # A walrus binding, not `row[name] for row in scored if row[name] is
        # not None`: mypy can't narrow a repeated dict subscript's type from
        # an `is not None` filter, only a named local variable's.
        values: list[float] = [value for row in scored if (value := row[name]) is not None]
        if not values:
            raise RuntimeError(
                f"Every {name} score failed - the judge LLM may not be usable for this "
                "metric. Try a stronger --judge-model."
            )
        return statistics.mean(values)

    failures = {
        name: sum(1 for row in scored if row[name] is None)
        for name in ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
    }

    return AnswerQualityResult(
        questions_evaluated=len(records),
        faithfulness=average("faithfulness"),
        answer_relevancy=average("answer_relevancy"),
        context_precision=average("context_precision"),
        context_recall=average("context_recall"),
        failures=failures,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--judge-model",
        default="llama3.2",
        help="Chat model used as the RAGAS judge, via Ollama's OpenAI-compatible API by default.",
    )
    args = parser.parse_args()

    settings = get_settings()
    judge_client = AsyncOpenAI(base_url=f"{settings.ollama_base_url}/v1", api_key="ollama")
    llm = llm_factory(args.judge_model, client=judge_client)
    embeddings = LiteLLMEmbeddings(
        model=settings.embedding_model, api_base=settings.ollama_base_url
    )

    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        upload_corpus(client)
        records = build_records(client, load_dataset(), top_k=args.top_k)

    result = asyncio.run(score_records(llm, embeddings, records))

    print(f"Questions evaluated: {result.questions_evaluated}")
    print(f"Faithfulness:      {result.faithfulness:.3f}")
    print(f"Answer relevancy:  {result.answer_relevancy:.3f}")
    print(f"Context precision: {result.context_precision:.3f}")
    print(f"Context recall:    {result.context_recall:.3f}")
    if any(result.failures.values()):
        print("\nFailures (excluded from the averages above, not counted as 0):")
        for name, count in result.failures.items():
            if count:
                print(f"  {name}: {count}/{result.questions_evaluated}")


if __name__ == "__main__":
    main()
