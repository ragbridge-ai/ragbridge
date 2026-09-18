"""Unit tests for the planner. No network calls, no database needed."""

import asyncio
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from ragbridge.agent.planner import (
    ANSWER_NOW,
    FakePlanner,
    LiteLLMPlanner,
    PlannerDecision,
    parse_decision,
)
from ragbridge.config import Settings


def test_parse_search_decision() -> None:
    decision = parse_decision('{"action": "search", "query": "cancellation policy"}')

    assert decision == PlannerDecision(action="search", query="cancellation policy")


def test_parse_search_decision_strips_the_query() -> None:
    decision = parse_decision('{"action": "search", "query": "  refunds \\n"}')

    assert decision.query == "refunds"


def test_parse_answer_decision() -> None:
    assert parse_decision('{"action": "answer"}') == ANSWER_NOW


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        '{"action": "search", "query": ',  # truncated
        '{"$defs": {}, "properties": {"action": {}}}',  # schema echoed back (ADR 0004)
        '["search", "refunds"]',  # valid JSON, wrong shape
        '"answer"',
        '{"query": "refunds"}',  # missing action
        '{"action": "browse", "query": "refunds"}',  # unknown action
        '{"action": null}',
        '{"action": "search"}',  # search without a query
        '{"action": "search", "query": ""}',
        '{"action": "search", "query": "   "}',
        '{"action": "search", "query": null}',
        '{"action": "search", "query": 42}',
    ],
)
def test_unusable_replies_fall_back_to_answering(raw: str) -> None:
    assert parse_decision(raw) == ANSWER_NOW


def test_fallback_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="ragbridge.agent.planner"):
        parse_decision("not json at all")

    assert "answering now" in caplog.text


def test_fake_planner_returns_its_script_in_order_then_answers() -> None:
    first = PlannerDecision(action="search", query="one")
    second = PlannerDecision(action="search", query="two")
    fake = FakePlanner([first, second])

    async def run() -> list[PlannerDecision]:
        return [await fake.plan("q", [], step, 3) for step in range(1, 5)]

    assert asyncio.run(run()) == [first, second, ANSWER_NOW, ANSWER_NOW]


def test_fake_planner_without_a_script_always_answers() -> None:
    assert asyncio.run(FakePlanner().plan("q", [], 1, 3)) == ANSWER_NOW


def _fake_completion(monkeypatch: pytest.MonkeyPatch, content: str | None) -> dict[str, Any]:
    """Replace litellm.acompletion; the returned dict records the call's kwargs."""
    calls: dict[str, Any] = {}

    async def acompletion(**kwargs: Any) -> Any:
        calls.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


def test_litellm_planner_parses_the_model_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_completion(monkeypatch, '{"action": "search", "query": "refunds"}')

    decision = asyncio.run(LiteLLMPlanner(Settings()).plan("q", ["a finding"], 1, 3))

    assert decision == PlannerDecision(action="search", query="refunds")


def test_litellm_planner_treats_an_empty_reply_as_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_completion(monkeypatch, None)

    assert asyncio.run(LiteLLMPlanner(Settings()).plan("q", [], 1, 3)) == ANSWER_NOW


def test_litellm_planner_uses_the_planner_model_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_completion(monkeypatch, '{"action": "answer"}')
    settings = Settings(chat_model="ollama/llama3.2", agent_planner_model="anthropic/strong")

    asyncio.run(LiteLLMPlanner(settings).plan("q", [], 1, 3))

    assert calls["model"] == "anthropic/strong"
    assert calls["api_base"] is None


def test_litellm_planner_falls_back_to_the_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_completion(monkeypatch, '{"action": "answer"}')
    settings = Settings(chat_model="ollama/llama3.2", agent_planner_model="")

    asyncio.run(LiteLLMPlanner(settings).plan("q", [], 1, 3))

    assert calls["model"] == "ollama/llama3.2"
    assert calls["api_base"] == settings.ollama_base_url


def test_litellm_planner_prompt_includes_question_findings_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_completion(monkeypatch, '{"action": "answer"}')

    asyncio.run(LiteLLMPlanner(Settings()).plan("What is X?", ["finding one"], 2, 3))

    user_message = calls["messages"][1]["content"]
    assert "What is X?" in user_message
    assert "finding one" in user_message
    assert "search 2 of at most 3" in user_message
