"""Smoke test for the retrieval evaluation script.

Proves the script still runs end to end after any change to retrieval or
the API - it does not measure retrieval quality. FakeEmbedder has no
semantics, so recall@k and MRR here are meaningless numbers, not a
regression signal (decision 5, docs/plans/phase-2.md); only their shape
(a fraction between 0 and 1, over every question) is asserted.

TestClient satisfies evaluate_retrieval's and upload_corpus's HttpClient
Protocol exactly as a real client pointed at a running server would - no
separate mock layer needed (see evaluate_retrieval.HttpClient's docstring
for why this is a structural Protocol, not a check against httpx.Client
by name).
"""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evaluation.evaluate_retrieval import (
    Question,
    evaluate_retrieval,
    load_dataset,
    upload_corpus,
)


def test_evaluation_script_runs_end_to_end(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    questions = load_dataset()

    upload_corpus(client)
    result = evaluate_retrieval(client, questions)

    assert result.questions_evaluated == len(questions)
    assert 0.0 <= result.recall_at_k <= 1.0
    assert 0.0 <= result.mrr <= 1.0


class _CannedResponse:
    def __init__(self, body: Any) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._body


class _CannedClient:
    """Answers every POST /query with the same sources."""

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self._sources = sources

    def post(self, url: str, *, json: Any = None, files: Any = None) -> _CannedResponse:
        return _CannedResponse({"answer": "", "sources": self._sources})


def _question() -> Question:
    return Question(
        question="Which?", source_document="a.md", source_snippet="the fact", reference_answer=""
    )


def test_a_context_only_source_is_not_counted_as_retrieved() -> None:
    """POST /query also lists the neighbours it gave the model; retrieval did not return them."""
    client = _CannedClient(
        [
            {"snippet": "something else", "context_only": False},
            {"snippet": "contains the fact", "context_only": True},
        ]
    )

    result = evaluate_retrieval(client, [_question()])

    assert result.recall_at_k == 0.0
    assert result.mrr == 0.0


def test_a_retrieved_source_still_counts_and_ranks_ahead_of_context_only_ones() -> None:
    client = _CannedClient(
        [
            {"snippet": "something else", "context_only": False},
            {"snippet": "contains the fact", "context_only": False},
            {"snippet": "contains the fact too", "context_only": True},
        ]
    )

    result = evaluate_retrieval(client, [_question()])

    assert result.recall_at_k == 1.0
    assert result.mrr == 0.5
