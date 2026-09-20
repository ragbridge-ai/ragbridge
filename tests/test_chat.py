"""Unit tests for the fake chatter. No network calls, no database needed."""

import asyncio

from ragbridge.chat import FakeChatter, build_messages


def test_fake_chatter_reports_how_many_chunks_it_used() -> None:
    chatter = FakeChatter()

    result = asyncio.run(chatter.answer("What is ragbridge?", ["chunk one", "chunk two"]))

    assert result == "Fake answer using 2 chunk(s)."


def test_fake_chatter_handles_empty_context() -> None:
    chatter = FakeChatter()

    result = asyncio.run(chatter.answer("What is ragbridge?", []))

    assert result == "Fake answer using 0 chunk(s)."


def test_the_prompt_tells_the_model_a_bullet_belongs_to_the_heading_above_it() -> None:
    messages = build_messages("Which company?", ["[cv.pdf, chunk 0]\ntext"])

    system = messages[0]["content"]
    assert "ABOVE" in system and "never" in system
    assert "not shown" in system or "missing" in system


def test_the_context_is_passed_to_the_model_exactly_as_built() -> None:
    context = [
        "[cv.pdf, chunk 0]\nfirst",
        "[... chunk 1 is not shown ...]\n[cv.pdf, chunk 2]\nthird",
    ]

    messages = build_messages("Which company?", context)

    assert messages[1]["content"] == (
        "Context:\n" + "\n\n".join(context) + "\n\nQuestion: Which company?"
    )


def test_no_context_says_so() -> None:
    messages = build_messages("Which company?", [])

    assert "(no relevant documents found)" in messages[1]["content"]
