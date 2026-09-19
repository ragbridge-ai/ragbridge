"""Tests for the agent evaluation script's corpus generator and scoring.

No server and no model: ``evaluate`` only needs something with a ``post`` method,
so a small in-memory fake stands in for the API. This proves the script measures
what it claims to; the numbers in docs/evaluation.md come from a real run.
"""

from typing import Any

from evaluation.evaluate_agent import (
    Case,
    Corpus,
    TierScore,
    build_corpus,
    evaluate,
    format_table,
)


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class FakeClient:
    """Answers every question with a fixed body and records what was asked."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[tuple[str, Any]] = []

    def post(self, url: str, *, json: Any = None, files: Any = None) -> FakeResponse:
        self.calls.append((url, json))
        return FakeResponse(self.body)


def test_the_corpus_is_deterministic_for_a_seed_and_differs_between_seeds() -> None:
    assert build_corpus(5, seed=1) == build_corpus(5, seed=1)
    assert build_corpus(5, seed=1) != build_corpus(5, seed=2)


def test_each_chain_has_four_documents_and_three_questions() -> None:
    corpus = build_corpus(6)

    assert len(corpus.documents) == 6 * 4
    assert len(corpus.cases) == 6 * 3
    assert [case.tier for case in corpus.cases[:3]] == ["1-hop", "2-hop", "3-hop"]


def test_every_answer_is_actually_written_in_the_document_it_points_to() -> None:
    """If this failed, a perfect system would score below 100%."""
    corpus = build_corpus(15)

    for case in corpus.cases:
        assert case.answer_email in corpus.documents[case.answer_document]


def test_names_are_unique_so_a_question_has_exactly_one_answer() -> None:
    corpus = build_corpus(15)

    emails = {case.answer_email for case in corpus.cases}
    # 15 chains x (owner + manager) = 30 distinct people, however many cases share one.
    assert len(emails) == 30


def test_a_multi_hop_question_does_not_name_the_person_whose_email_is_the_answer() -> None:
    """The point of the corpus: the answer cannot be read off the question.

    The 1-hop control is the exception on purpose - it names the person, which is
    what makes it the easy baseline.
    """
    for case in build_corpus(15).cases:
        person = case.answer_email.split("@")[0].replace(".", " ")
        if case.tier == "1-hop":
            assert person in case.question.lower()
        else:
            assert person not in case.question.lower()
            assert "@" not in case.question


def _corpus_with_one_case() -> Corpus:
    case = Case("3-hop", "Who?", "boss@example.test", "email-manager-0.txt")
    return Corpus(documents={}, cases=[case])


def test_retrieved_and_correct_are_scored_independently() -> None:
    body = {
        "answer": "It is BOSS@example.test.",
        "sources": [{"filename": "other.txt"}, {"filename": "email-manager-0.txt"}],
    }

    scores = evaluate(FakeClient(body), _corpus_with_one_case(), "query")

    assert (scores["3-hop"].retrieved, scores["3-hop"].correct) == (1, 1)


def test_a_wrong_answer_with_the_right_document_is_retrieved_but_not_correct() -> None:
    body = {"answer": "I don't know.", "sources": [{"filename": "email-manager-0.txt"}]}

    scores = evaluate(FakeClient(body), _corpus_with_one_case(), "query")

    assert (scores["3-hop"].retrieved, scores["3-hop"].correct) == (1, 0)


def test_a_lucky_answer_without_the_document_is_correct_but_not_retrieved() -> None:
    body = {"answer": "boss@example.test", "sources": [{"filename": "other.txt"}]}

    scores = evaluate(FakeClient(body), _corpus_with_one_case(), "query")

    assert (scores["3-hop"].retrieved, scores["3-hop"].correct) == (0, 1)


def test_the_agent_endpoint_is_called_and_its_steps_are_recorded() -> None:
    body = {"answer": "x", "sources": [], "step_count": 3}
    client = FakeClient(body)

    scores = evaluate(client, _corpus_with_one_case(), "agent", repeats=2)

    assert [url for url, _ in client.calls] == ["/agent", "/agent"]
    assert scores["3-hop"].steps == [3, 3]
    assert scores["3-hop"].mean_steps == 3.0


def test_the_query_endpoint_is_called_with_top_k_five_and_records_no_steps() -> None:
    client = FakeClient({"answer": "x", "sources": []})

    scores = evaluate(client, _corpus_with_one_case(), "query")

    assert client.calls == [("/query", {"question": "Who?", "top_k": 5})]
    assert scores["3-hop"].mean_steps is None


def test_rates_of_an_empty_tier_are_zero_not_a_division_error() -> None:
    assert TierScore().retrieved_rate == 0.0
    assert TierScore().correct_rate == 0.0


def test_the_table_has_a_row_per_endpoint_and_tier() -> None:
    body = {"answer": "boss@example.test", "sources": [], "step_count": 2}
    corpus = _corpus_with_one_case()

    table = format_table(
        {
            "query": evaluate(FakeClient(body), corpus, "query"),
            "agent": evaluate(FakeClient(body), corpus, "agent"),
        }
    )

    assert table.count("\n") + 1 == 2 + 2 * 3  # header + rule + 2 endpoints x 3 tiers
    assert "| `/agent` | 3-hop | 1 | 0% (0/1) | 100% (1/1) | 2.00 |" in table
