"""Smoke test for the retrieval evaluation script.

Proves the script still runs end to end after any change to retrieval or
the API - it does not measure retrieval quality. FakeEmbedder has no
semantics, so recall@k and MRR here are meaningless numbers, not a
regression signal (decision 5, docs/plans/phase-2.md); only their shape
(a fraction between 0 and 1, over every question) is asserted.

TestClient subclasses httpx.Client, so it satisfies evaluate_retrieval's
and upload_corpus's type exactly as a real client pointed at a running
server would - no separate mock layer needed.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evaluation.evaluate_retrieval import evaluate_retrieval, load_dataset, upload_corpus


def test_evaluation_script_runs_end_to_end(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    questions = load_dataset()

    upload_corpus(client)
    result = evaluate_retrieval(client, questions)

    assert result.questions_evaluated == len(questions)
    assert 0.0 <= result.recall_at_k <= 1.0
    assert 0.0 <= result.mrr <= 1.0
