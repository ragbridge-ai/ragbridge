"""Tests for the ranking evaluation: the corpus, the metrics, and a smoke run.

The smoke run uses ``FakeEmbedder``, which has no semantics, so the numbers it
produces mean nothing; only their shape is asserted (decision 5, docs/plans/phase-1.md).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evaluation.evaluate_ranking import (
    MODES,
    Case,
    CaseResult,
    evaluate_agent,
    evaluate_search,
    load_cases,
    ranks_of,
    summarise,
    upload_corpus,
)
from evaluation.ranking_corpus import DISTRACTORS, FACTS, PRODUCT, TABLE_PATH, build_documents
from ragbridge.chunking import chunk_text


def _all_chunks() -> list[str]:
    return [
        chunk
        for text in build_documents().values()
        for chunk in chunk_text(text, chunk_size=1000, chunk_overlap=200, min_size=100)
    ]


def test_the_corpus_has_about_a_hundred_chunks() -> None:
    assert 95 <= len(_all_chunks()) <= 110


def test_the_corpus_is_the_same_every_time() -> None:
    assert build_documents() == build_documents()


def test_the_long_table_chunk_is_the_verbatim_table_and_stays_one_chunk() -> None:
    table = TABLE_PATH.read_text()
    chunks = chunk_text(build_documents()["tech-stack.md"], chunk_size=1000, chunk_overlap=200)

    assert chunks == [table.strip()]
    assert len(table.strip().splitlines()) - 2 >= 18, "a real table of about eighteen rows"
    assert "| Web framework | FastAPI + Uvicorn |" in table


def test_most_chunks_are_generic_and_repeat_the_product_name() -> None:
    chunks = _all_chunks()
    generic = [chunk for chunk in chunks if chunk.count(PRODUCT) >= 3]

    assert len(generic) >= 90
    assert len(FACTS) == 4 and len(DISTRACTORS) == 4


def test_every_paragraph_stays_a_chunk_of_its_own() -> None:
    """Each is longer than the minimum chunk size, so none is merged into another."""
    paragraphs = [
        paragraph for text in build_documents().values() for paragraph in text.strip().split("\n\n")
    ]

    assert len(_all_chunks()) == len(paragraphs)


def test_every_expected_marker_identifies_exactly_one_chunk() -> None:
    chunks = _all_chunks()

    for case in load_cases():
        for marker in case.expected:
            assert sum(marker in chunk for chunk in chunks) == 1, (case.id, marker)


def test_the_dataset_covers_short_and_long_with_and_without_the_product_name() -> None:
    cases = load_cases()
    combinations = {(case.length, case.product_name) for case in cases}

    assert combinations == {
        ("short", "with"),
        ("short", "without"),
        ("long", "with"),
        ("long", "without"),
    }
    assert {"one", "two", "two-chunks", "control"} <= {case.facts for case in cases}
    for case in cases:
        assert (PRODUCT in case.question) == (case.product_name == "with"), case.id


def test_ranks_are_one_based_and_none_when_a_marker_is_absent() -> None:
    assert ranks_of(["b", "z"], ["a", "xxbxx", "c"]) == [2, None]


def _result(ranks: list[int | None]) -> CaseResult:
    case = Case("q", "long", "with", "two", "Q?", ["a"] * len(ranks))
    return CaseResult(case, ranks)


def test_recall_complete_and_reciprocal_rank() -> None:
    result = _result([2, 7])

    assert result.recall_at(1) == 0.0
    assert result.recall_at(5) == 0.5
    assert result.recall_at(10) == 1.0
    assert result.complete_at(5) == 0.0
    assert result.complete_at(10) == 1.0
    assert result.reciprocal_rank == 0.5


def test_a_question_with_nothing_found_scores_zero() -> None:
    result = _result([None])

    assert result.recall_at(10) == 0.0
    assert result.reciprocal_rank == 0.0


def test_summary_averages_over_questions() -> None:
    summary = summarise([_result([1]), _result([None])])

    assert summary["recall@1"] == 0.5
    assert summary["MRR"] == 0.5


def test_the_evaluation_runs_end_to_end_for_every_mode_and_for_the_agent(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    cases = load_cases()

    upload_corpus(client)
    for mode in MODES:
        results = evaluate_search(client, cases, mode)
        assert len(results) == len(cases)
        assert all(0.0 <= r.reciprocal_rank <= 1.0 for r in results)
    agent_results, steps = evaluate_agent(client, cases)

    assert len(agent_results) == len(steps) == len(cases)
    assert all(step >= 1 for step in steps)
