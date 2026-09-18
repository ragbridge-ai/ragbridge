"""Smoke test for the answer-quality evaluation script.

Only covers build_records (upload the corpus, call POST /query, assemble
records) - it never calls score_records. Unlike Embedder, Chatter, and
Reranker, RAGAS's metrics have no Fake implementation to call instead of
a real judge LLM: "is this answer faithful to its context" has no
algorithmic answer, only a real model's. Real scores need
evaluate_answers.main(), run by hand (decision 5, docs/plans/phase-2.md).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evaluation.evaluate_answers import build_records
from evaluation.evaluate_retrieval import load_dataset, upload_corpus


def test_build_records_assembles_the_four_ragas_fields(app_with_database: FastAPI) -> None:
    client = TestClient(app_with_database)
    questions = load_dataset()

    upload_corpus(client)
    records = build_records(client, questions)

    assert len(records) == len(questions)
    for record, question in zip(records, questions, strict=True):
        assert set(record) == {"user_input", "response", "retrieved_contexts", "reference"}
        assert record["user_input"] == question.question
        assert isinstance(record["response"], str) and record["response"]
        # Not just isinstance(..., list): retrieved_contexts must actually
        # contain something, or a caller forgetting to upload the corpus
        # first (a real bug, caught by running the real script - see the
        # `build: add ragas` commit and docs/plans/phase-2.md) would pass
        # this test vacuously with an empty list every time.
        assert len(record["retrieved_contexts"]) > 0
        assert record["reference"] == question.reference_answer
