"""Tests for the mechanical question-answering scorer.

No server, no model. What matters here is that the *scoring* is trustworthy:
every check can actually be passed, a wrong or evasive answer does not pass, and
the "I don't know" detector neither misses real abstentions nor fires on real
answers. A scorer that only ever says "pass" would make every model look good.
"""

import json
from typing import Any

import pytest

from evaluation.evaluate_qa import (
    CATEGORIES,
    Check,
    abstained,
    evaluate,
    format_table,
    load_checks,
    passes,
)
from evaluation.evaluate_retrieval import DATASET_PATH

CHECKS = load_checks()
ANSWERABLE = [check for check in CHECKS if check.category != "abstain"]


def test_there_are_43_checks_in_three_kinds() -> None:
    assert len(CHECKS) == 43
    assert {c: sum(1 for x in CHECKS if x.category == c) for c in CATEGORIES} == {
        "lookup": 25,
        "paraphrase": 10,
        "abstain": 8,
    }


def test_check_ids_are_unique() -> None:
    assert len({check.id for check in CHECKS}) == len(CHECKS)


def test_the_lookup_questions_are_exactly_the_phase_2_dataset() -> None:
    phase_2 = [json.loads(line)["question"] for line in DATASET_PATH.read_text().splitlines()]

    assert [c.question for c in CHECKS if c.category == "lookup"] == phase_2


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.id)
def test_every_checks_own_good_example_passes_it(check: Check) -> None:
    """If this failed, a perfect model would score below 100%."""
    assert passes(check, check.good_example)


@pytest.mark.parametrize("check", ANSWERABLE, ids=lambda check: check.id)
def test_an_evasive_answer_never_passes_an_answerable_question(check: Check) -> None:
    assert not passes(check, "I don't know.")
    assert not passes(check, "Something completely unrelated about weather.")


@pytest.mark.parametrize("check", [c for c in CHECKS if c.category == "lookup"], ids=lambda c: c.id)
def test_the_phase_2_reference_answers_pass_their_lookup_checks(check: Check) -> None:
    reference = {
        json.loads(line)["question"]: json.loads(line)["reference_answer"]
        for line in DATASET_PATH.read_text().splitlines()
    }[check.question]

    assert passes(check, reference)


@pytest.mark.parametrize(
    "answer",
    [
        "I don't know.",
        "I do not know the answer.",
        "The documents don't mention that.",
        "That is not mentioned in the context.",
        "There is no information about it.",
        "I cannot find that in the provided text.",
        "The policy does not specify.",
        "I'm not sure.",
    ],
)
def test_abstention_phrases_are_recognised(answer: str) -> None:
    assert abstained(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Employees get 22 days of paid vacation.",
        "Refunds are processed within 5 business days.",
        "You can park in the courtyard.",
    ],
)
def test_ordinary_answers_are_not_mistaken_for_abstentions(answer: str) -> None:
    assert not abstained(answer)


def test_an_abstain_question_passes_only_when_the_model_says_it_does_not_know() -> None:
    check = next(c for c in CHECKS if c.category == "abstain")

    assert passes(check, "I don't know.")
    assert not passes(check, "The CEO is Jane Smith.")


def test_a_must_not_match_pattern_rejects_a_confidently_wrong_answer() -> None:
    check = next(c for c in CHECKS if c.id == "P01")

    assert passes(check, "No. The window is 14 days.")
    assert not passes(check, "Yes, within 14 days you can.")


def test_matching_is_case_insensitive() -> None:
    check = next(c for c in CHECKS if c.id == "L08")

    assert passes(check, "Email BILLING@ACMECLOUD.EXAMPLE with your invoice number.")


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class ScriptedClient:
    """Answers each question with ``answers[question]``, else 'I don't know.'."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def post(self, url: str, *, json: Any = None, files: Any = None) -> FakeResponse:
        return FakeResponse({"answer": self.answers.get(json["question"], "I don't know.")})


def test_a_perfect_model_scores_100_percent_everywhere() -> None:
    perfect = {check.question: check.good_example for check in CHECKS}

    scores = evaluate(ScriptedClient(perfect), CHECKS)

    assert all(scores[c].passed == scores[c].asked for c in CATEGORIES)
    assert all(scores[c].wrongly_abstained == 0 for c in CATEGORIES)


def test_a_model_that_always_says_it_does_not_know_passes_only_abstain_questions() -> None:
    """The failure mode this scorer must expose: safe-looking, useless."""
    scores = evaluate(ScriptedClient({}), CHECKS)

    assert scores["abstain"].rate == 1.0
    assert scores["lookup"].passed == 0
    assert scores["paraphrase"].passed == 0
    assert scores["lookup"].wrongly_abstained == 25


def test_a_model_that_invents_answers_fails_the_abstain_questions() -> None:
    inventive = {c.question: "It is 42." for c in CHECKS if c.category == "abstain"}

    scores = evaluate(ScriptedClient(inventive), CHECKS)

    assert scores["abstain"].passed == 0


def test_failures_are_recorded_with_the_question_and_the_answer() -> None:
    scores = evaluate(ScriptedClient({}), CHECKS)

    assert any("L01" in f and "I don't know" in f for f in scores["lookup"].failures)


def test_the_table_names_the_model_and_every_kind() -> None:
    table = format_table("ollama/test:1b", evaluate(ScriptedClient({}), CHECKS))

    assert "Model: ollama/test:1b" in table
    assert all(f"| {c} |" in table for c in CATEGORIES)
