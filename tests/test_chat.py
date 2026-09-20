"""Unit tests for the fake chatter. No network calls, no database needed."""

import asyncio
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from ragbridge.chat import FakeChatter, LiteLLMChatter, build_messages
from ragbridge.config import Settings


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


def _fake_completion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace litellm.acompletion; the returned dict records the call's kwargs."""
    calls: dict[str, Any] = {}

    async def acompletion(**kwargs: Any) -> Any:
        calls.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


def test_the_answer_model_is_asked_for_temperature_zero_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_completion(monkeypatch)

    answer = asyncio.run(LiteLLMChatter(Settings()).answer("Which company?", ["context"]))

    assert answer == "ok"
    assert calls["temperature"] == 0.0


def test_the_configured_temperature_is_passed_to_the_answer_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_completion(monkeypatch)

    asyncio.run(LiteLLMChatter(Settings(chat_temperature=0.7)).answer("q", ["context"]))

    assert calls["temperature"] == 0.7


def test_a_provider_that_rejects_the_temperature_parameter_does_not_receive_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM drops a parameter the model does not support instead of raising."""
    calls = _fake_completion(monkeypatch)

    asyncio.run(LiteLLMChatter(Settings()).answer("q", ["context"]))

    assert calls["drop_params"] is True
