"""Evaluate retrieval quality against the evaluation/ corpus and dataset.

Recall@k and MRR need only an embedder, not an LLM judge (answer-quality
metrics come later, with RAGAS - Phase 2 step 5), so these are cheap
enough to run on every retrieval change. Everything here goes through the
real POST /documents and POST /query endpoints (docs/plans/phase-2.md,
step 4) - which is what lets the exact same functions serve a real
evaluation (against a live server) and the CI smoke test (against
TestClient with fakes, see tests/test_evaluate_retrieval.py): both
satisfy this module's HttpClient Protocol.

Run for real, against a running server with a real embedder configured:

    uv run python -m evaluation.evaluate_retrieval --base-url http://localhost:8000

CI never does this (Phase 1 decision 5) - only the smoke test runs there.
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

CORPUS_DIR = Path(__file__).parent / "corpus"
DATASET_PATH = Path(__file__).parent / "dataset.jsonl"


class HttpResponse(Protocol):
    """The parts of an HTTP response these evaluation scripts use."""

    def raise_for_status(self) -> object: ...
    def json(self) -> Any: ...


class HttpClient(Protocol):
    """The one method these evaluation scripts need from an HTTP client.

    Deliberately structural, not httpx.Client by name - the same "depend
    on the shape, not a concrete class" idiom as ragbridge.embeddings.
    Embedder. A real httpx.Client (pointed at a running server) and
    starlette's TestClient (the CI smoke test) both satisfy this, but
    TestClient's *nominal* base class is not guaranteed to stay
    httpx.Client: installing ragas (Phase 2 step 5) transitively pulls in
    httpx2 (via langchain-core -> langsmith), and once httpx2 is
    importable, TestClient switches to subclassing httpx2.Client instead.
    Discovered while building step 5, not assumed - see
    docs/plans/phase-2.md.

    Lists json and files by name, typed loosely, rather than **kwargs:
    Any - mypy's structural matching for a Protocol method with **kwargs
    requires the implementation to also declare **kwargs, so it rejects
    httpx.Client.post's real signature (many specific named parameters)
    even though that signature accepts a superset of what this Protocol
    needs. Naming exactly the two keyword arguments these scripts pass
    sidesteps that mismatch (verified: this is what actually made mypy
    accept a real httpx.Client and TestClient here).
    """

    def post(self, url: str, *, json: Any = None, files: Any = None) -> HttpResponse: ...


@dataclass(frozen=True)
class Question:
    question: str
    source_document: str
    source_snippet: str
    """A short, verbatim quote from the paragraph this question is about.

    Finding it inside a response's sources[].snippet is how this script
    tells "the right chunk came back" (docs/plans/phase-2.md, step 4).
    """
    reference_answer: str
    """A short, ground-truth answer to the question, grounded in the same
    paragraph as source_snippet. Unused by this script - it exists for
    evaluate_answers.py's RAGAS metrics (step 5), which need a reference
    answer to compare a generated answer and its retrieved context
    against.
    """


@dataclass(frozen=True)
class EvaluationResult:
    questions_evaluated: int
    recall_at_k: float
    """Fraction of questions where the right chunk appeared anywhere in top_k."""
    mrr: float
    """Mean reciprocal rank: 1/rank when the right chunk is found, 0 when it
    is not, averaged over every question - rewards finding it near the top,
    not merely finding it somewhere in the list.
    """


def load_dataset(path: Path = DATASET_PATH) -> list[Question]:
    """Load the question dataset from a JSON Lines file."""
    questions: list[Question] = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        questions.append(
            Question(
                question=row["question"],
                source_document=row["source_document"],
                source_snippet=row["source_snippet"],
                reference_answer=row["reference_answer"],
            )
        )
    return questions


def upload_corpus(client: HttpClient, corpus_dir: Path = CORPUS_DIR) -> None:
    """Upload every corpus document through POST /documents."""
    for path in sorted(corpus_dir.glob("*.md")):
        response = client.post(
            "/documents", files={"file": (path.name, path.read_bytes(), "text/markdown")}
        )
        response.raise_for_status()


def evaluate_retrieval(
    client: HttpClient, questions: list[Question], *, top_k: int = 5
) -> EvaluationResult:
    """Run every question through POST /query and score retrieval.

    A question counts as a hit if its source_snippet appears in the
    snippet of one of the top_k returned sources; its reciprocal rank is
    1 / that source's position, or 0 if no source matches.
    """
    reciprocal_ranks: list[float] = []

    for question in questions:
        response = client.post("/query", json={"question": question.question, "top_k": top_k})
        response.raise_for_status()
        sources = response.json()["sources"]

        rank = next(
            (
                position
                for position, source in enumerate(sources, start=1)
                if question.source_snippet in source["snippet"]
            ),
            None,
        )
        reciprocal_ranks.append(1 / rank if rank is not None else 0.0)

    hits = sum(1 for reciprocal_rank in reciprocal_ranks if reciprocal_rank > 0)
    return EvaluationResult(
        questions_evaluated=len(questions),
        recall_at_k=hits / len(questions),
        mrr=sum(reciprocal_ranks) / len(questions),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--api-key", required=True, help="API key created with ragbridge-admin.")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=60.0) as client:
        upload_corpus(client)
        result = evaluate_retrieval(client, load_dataset(), top_k=args.top_k)

    print(f"Questions evaluated: {result.questions_evaluated}")
    print(f"Recall@{args.top_k}: {result.recall_at_k:.2%}")
    print(f"MRR: {result.mrr:.3f}")


if __name__ == "__main__":
    main()
