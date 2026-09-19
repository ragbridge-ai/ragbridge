"""Tests for the pure parts of the model speed measurement.

The measurement itself needs a running Ollama, so it is not run here; what can be
checked without one is the arithmetic and the table, which is where a silent unit
mistake (Ollama reports nanoseconds) would produce plausible-looking wrong numbers.
"""

from evaluation.measure_model_speed import Speed, format_table, rag_sized_prompt, tokens_per_second


def test_tokens_per_second_converts_from_nanoseconds() -> None:
    assert tokens_per_second(150, 3_000_000_000) == 50.0


def test_tokens_per_second_of_nothing_timed_is_zero_not_a_division_error() -> None:
    assert tokens_per_second(10, 0) == 0.0


def test_a_rag_sized_prompt_contains_the_corpus_and_a_question() -> None:
    prompt = rag_sized_prompt()

    assert "Refund window" in prompt
    assert prompt.endswith("Question: How many days of paid vacation do employees get?")
    assert 3_000 < len(prompt) < 12_000  # roughly a thousand tokens, as five chunks would be


def test_the_table_has_a_row_per_model_with_rounded_values() -> None:
    result = Speed("m:1b", 3.14, 4.74, 100.0, 25.6, 230.4, 1206, 6.04)

    table = format_table([result])

    assert "| m:1b | 26 | 230 | 6.0 s (~1206 tokens) | 3.1 s | 4.74 GB | 100% |" in table
    assert table.count("\n") == 2
